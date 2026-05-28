import time
import structlog

from app.core.config import get_settings
from app.core.embeddings import embed_query
from app.core import vector_store as vs
from app.core import graph as graph_db
from app.core import llm
from app.services.git_fetcher import fetch_fragment
from app.models.schemas import QueryRequest, QueryResponse, QueryResult
from app.prompts import load_prompt

log = structlog.get_logger()
settings = get_settings()

_repo_url_cache: dict[str, str] = {}
# (repo, module_path) → latest tag for that specific module. Cleared on indexing.
_module_latest_tag_cache: dict[tuple[str, str], str] = {}
# Cached module catalog (one full DB scan, refreshed when invalidate_caches() runs)
_module_catalog_cache: str | None = None
_module_catalog_cache_at: float = 0.0
_MODULE_CATALOG_TTL_SECONDS = 600  # 10 minutes


def _semver_key(v: str) -> tuple:
    """Extract (major, minor, patch) tuple for sorting; non-semver → (0,0,0)."""
    import re
    m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', v)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)) if m else (0, 0, 0)


async def _get_repo_url(db, repo_name: str) -> str:
    """Get repo URL (cached per repo)."""
    if repo_name not in _repo_url_cache:
        from sqlalchemy import text
        url_result = await db.execute(
            text("SELECT repo_url FROM index_jobs WHERE repo = :repo AND repo_url IS NOT NULL ORDER BY started_at DESC LIMIT 1"),
            {"repo": repo_name},
        )
        _repo_url_cache[repo_name] = url_result.scalar() or ""
    return _repo_url_cache[repo_name]


async def _get_module_latest_tag(db, repo: str, module_path: str) -> str:
    """Get the latest (semver-sorted) version tag for a specific module.

    Per-module (not per-repo) lookup — fixes the bug where every module in a
    monorepo would receive the same ?ref pointing to some unrelated global tag.
    Excludes branch refs (main/master/HEAD) which are not pinned versions.
    """
    cache_key = (repo, module_path)
    if cache_key in _module_latest_tag_cache:
        return _module_latest_tag_cache[cache_key]

    from sqlalchemy import text
    result = await db.execute(
        text(r"""
            SELECT DISTINCT version FROM modules
            WHERE repo = :repo
              AND module_path = :module_path
              AND version ~ '[0-9]+\.[0-9]'
              AND version !~ '^(main|master|HEAD|develop)$'
        """),
        {"repo": repo, "module_path": module_path},
    )
    versions = [r["version"] for r in result.mappings().all()]
    versions.sort(key=_semver_key, reverse=True)
    latest = versions[0] if versions else ""
    _module_latest_tag_cache[cache_key] = latest
    return latest


async def _get_repo_info(db, repo_name: str) -> dict:
    """Backwards-compatible wrapper — kept for any external callers.

    NOTE: latest_tag here is best-effort per-REPO and may be wrong for
    monorepos. Prefer _get_module_latest_tag(db, repo, module_path) for
    accuracy. Returns {"url": ..., "latest_tag": ""} (empty tag) to discourage
    use of the per-repo tag.
    """
    return {"url": await _get_repo_url(db, repo_name), "latest_tag": ""}


def _build_module_source(m: dict) -> str:
    """Build Terraform-compatible source path for a module.

    Expects `_repo_url` and `_latest_tag` to be set on the module dict.
    Callers should populate `_latest_tag` via `_get_module_latest_tag`
    (per-module) — NOT per-repo, otherwise every module in a monorepo gets
    the same unrelated tag.
    """
    repo_url = m.get("_repo_url", "")
    module_path = m.get("module_path", "")
    latest_tag = m.get("_latest_tag", "")

    if repo_url:
        # git@github.com:org/repo.git → git::ssh://git@github.com/org/repo.git
        if repo_url.startswith("git@"):
            url = "git::ssh://" + repo_url.replace(":", "/", 1)
        else:
            url = repo_url
        ref = f"?ref={latest_tag}" if latest_tag else ""
        return f"{url}//{module_path}{ref}"

    return f"{m.get('repo', '')}//{module_path}"


def invalidate_caches() -> None:
    """Clear in-memory caches. Call after indexing or migrations."""
    global _module_catalog_cache, _module_catalog_cache_at
    _repo_url_cache.clear()
    _module_latest_tag_cache.clear()
    _module_catalog_cache = None
    _module_catalog_cache_at = 0.0


# Heuristic groups for the module catalog. Each module is assigned to ONE
# group by matching the regex against `tags` or `module_path`. First match wins.
_CATALOG_GROUPS: list[tuple[str, str]] = [
    ("Networking",       r"vpc|subnet|nat|route53|cloudfront|peering"),
    ("Load balancers",   r"alb|nlb|elb|target_group|listener"),
    ("Security & IAM",   r"security_group|iam|secrets|kms|acm|waf"),
    ("Compute / ECS",    r"ecs|fargate|microservice|cluster|task"),
    ("Compute / Lambda", r"lambda|step_function"),
    ("Compute / EC2",    r"ec2|autoscal|launchtemplate|asg"),
    ("Storage",          r"s3|efs|ebs|backup"),
    ("Databases",        r"rds|aurora|dynamodb|elasticache|redis"),
    ("Messaging & Queue",r"sqs|sns|eventbridge|kinesis|api_gateway"),
    ("Observability",    r"cloudwatch|logs|alarm|metric"),
    ("Other",            r".*"),
]


async def _build_module_catalog(db) -> str:
    """Return a human-readable catalog of available modules, grouped by domain.

    Cached for _MODULE_CATALOG_TTL_SECONDS so we don't re-query on every
    request. Cache is busted by invalidate_caches() after re-indexing.
    """
    global _module_catalog_cache, _module_catalog_cache_at
    import time as _time
    import re as _re
    from sqlalchemy import text

    if (_module_catalog_cache is not None
            and _time.time() - _module_catalog_cache_at < _MODULE_CATALOG_TTL_SECONDS):
        return _module_catalog_cache

    # One representative row per (repo, module_path) — the freshest description.
    # Exclude /examples/ paths — those are showcase code, not reusable modules.
    # Join with usage counts so the catalog can flag modules nobody uses.
    result = await db.execute(
        text("""
            SELECT DISTINCT ON (m.repo, m.module_path)
                m.repo, m.module_path, m.tags,
                COALESCE(m.description, '') AS description,
                COALESCE(u.usage_count, 0) AS usage_count
            FROM modules m
            LEFT JOIN (
                SELECT module_ref, COUNT(*) AS usage_count
                FROM knowledge_snippets
                WHERE kind = 'usage'
                GROUP BY module_ref
            ) u ON u.module_ref = m.repo || '/' || m.module_path
            WHERE m.description IS NOT NULL AND m.description != ''
              AND m.module_path NOT LIKE '%%/examples/%%'
              AND m.module_path NOT LIKE 'examples/%%'
            ORDER BY m.repo, m.module_path, m.indexed_at DESC
        """),
    )
    rows = [dict(r) for r in result.mappings().all()]

    # Enrich each module with its latest version tag (semver-aware)
    for r in rows:
        r["_latest_tag"] = await _get_module_latest_tag(
            db, r["repo"], r["module_path"],
        )

    # Compile group regexes
    compiled = [(g, _re.compile(rx, _re.IGNORECASE)) for g, rx in _CATALOG_GROUPS]

    def _group_for(row: dict) -> str:
        haystack = " ".join(row.get("tags") or []) + " " + row["module_path"]
        for group, rx in compiled:
            if rx.search(haystack):
                return group
        return "Other"

    groups: dict[str, list[dict]] = {g: [] for g, _ in _CATALOG_GROUPS}
    for row in rows:
        # Take first half-sentence of description (cap 75 chars)
        desc = row["description"].split("\n", 1)[0].split(". ", 1)[0].strip()
        if desc and len(desc) > 75:
            desc = desc[:72] + "..."
        groups[_group_for(row)].append({"row": row, "desc": desc})

    # Sort each group: used modules first, then top-level (fewer slashes),
    # then alphabetic. This lifts well-known modules like
    # `my-modules-repo//vpc` above unused/example ones.
    for group in groups:
        groups[group].sort(
            key=lambda e: (
                # Bucket 0: has usage  ·  Bucket 1: no usage (unused)
                0 if (e["row"].get("usage_count") or 0) > 0 else 1,
                e["row"]["module_path"].count("/"),
                e["row"]["module_path"],
            ),
        )

    parts = ["## Organisation module catalog (USE THESE — do not write raw resources for these domains)"]
    for group, _rx in _CATALOG_GROUPS:
        items = groups[group]
        if not items:
            continue
        # Cap each group at 15 modules to keep prompt manageable
        capped = items[:15]
        lines = []
        for e in capped:
            tag = e["row"].get("_latest_tag", "")
            ref = f"?ref={tag}" if tag else ""
            usage_count = e["row"].get("usage_count", 0) or 0
            # Mark modules with zero recorded usage — they may be unmaintained,
            # niche, or deprecated. The LLM should prefer alternatives if any.
            unused_flag = " [UNUSED IN ANY DEPLOYMENT]" if usage_count == 0 else ""
            base = f"  - {e['row']['repo']}/{e['row']['module_path']}{ref}{unused_flag}"
            if e["desc"]:
                base += f" — {e['desc']}"
            lines.append(base)
        if len(items) > 15:
            lines.append(f"  - ... and {len(items) - 15} more")
        parts.append(f"\n### {group}")
        parts.extend(lines)

    catalog = "\n".join(parts)
    _module_catalog_cache = catalog
    _module_catalog_cache_at = _time.time()
    log.info("module_catalog_built",
             chars=len(catalog),
             groups=sum(1 for g, _ in _CATALOG_GROUPS if groups[g]),
             total_modules=sum(len(v) for v in groups.values()))
    return catalog


# Helper: is this query_type one that should use the full generate pipeline
# (multi-query, dep expansion, compose patterns, two-step shopping list,
# module catalog)? Both `generate` and `compose` use the same machinery —
# `compose` additionally tells the LLM that the task is multi-module.
def _is_compose_mode(query_type: str) -> bool:
    # "generate" kept as alias for backwards compatibility (API, MCP)
    return query_type in ("generate", "compose")


SYSTEM_PROMPTS = {
    t: load_prompt(f"retriever/{t}.md")
    for t in ("compose", "generate", "optimize", "audit", "search")
}


_SHOPPING_LIST_SYSTEM_PROMPT = load_prompt("retriever/shopping_list.md")


async def _build_shopping_list(
    db, user_query: str, similar: list[dict],
    compose_context: str,
) -> list[tuple[str, str]]:
    """Step 1 of two-step generation: ask a cheap LLM to pick concrete modules.

    Returns a list of (repo, module_path) tuples that the next step will
    fetch in full detail. Empty list on failure (caller falls back to the
    one-step flow).
    """
    import json as _json

    # Compact context: just module refs + brief description, plus the
    # stack/compose hints. The full prompt with all variable/output details
    # comes in step 2.
    refs_lines = []
    for r in similar[:30]:
        ref = f"{r['repo']}//{r['module_path']}"
        desc = (r.get("description") or "").split("\n", 1)[0][:90]
        refs_lines.append(f"  - {ref} — {desc}")
    refs_block = "\n".join(refs_lines)

    prompt_parts = [f"User request:\n{user_query}\n"]
    if refs_block:
        prompt_parts.append(f"Top semantic matches:\n{refs_block}\n")
    if compose_context:
        prompt_parts.append(compose_context + "\n")
    prompt = "\n".join(prompt_parts)

    try:
        raw = await llm.adescribe(
            prompt, system=_SHOPPING_LIST_SYSTEM_PROMPT, max_tokens=400,
        )
    except Exception as exc:
        log.warning("shopping_list_llm_failed", error=str(exc)[:200])
        return []
    if not raw:
        return []

    cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        items = _json.loads(cleaned)
    except Exception:
        log.warning("shopping_list_parse_failed", raw=raw[:200])
        return []
    if not isinstance(items, list):
        return []

    out: list[tuple[str, str]] = []
    for s in items:
        if not isinstance(s, str) or "//" not in s:
            continue
        repo, _, path = s.partition("//")
        repo, path = repo.strip("/"), path.strip("/")
        if repo and path:
            out.append((repo, path))
        if len(out) >= 12:
            break
    log.info("shopping_list_built", count=len(out), items=out)
    return out


async def _fetch_module_details(
    db, refs: list[tuple[str, str]]
) -> list[dict]:
    """Fetch full module rows (with variables + outputs) for shopping-list refs."""
    out: list[dict] = []
    for repo, module_path in refs:
        row = await vs.get_module_by_path(db, repo, module_path)
        if row:
            row = dict(row)
            row["similarity"] = 0.95  # treat as high-priority
            out.append(row)
    return out


def _dedupe_modules(rows: list[dict]) -> list[dict]:
    """Deduplicate module rows by (repo, module_path), keeping highest similarity."""
    best: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["repo"], r["module_path"])
        if key not in best or r.get("similarity", 0) > best[key].get("similarity", 0):
            best[key] = r
    return sorted(best.values(), key=lambda r: r.get("similarity", 0), reverse=True)


async def _expand_with_dependencies(db, modules: list[dict], hops: int = 3) -> list[dict]:
    """For the top N modules, add their direct (1-hop) dependencies.

    Returns the original list + dependency rows (fetched from PostgreSQL by
    (repo, module_path)), deduplicated. Direct deps are marked with
    similarity=0.5 — high enough to be included but lower than the
    semantically-matched primaries.
    """
    if not modules:
        return modules

    # Collect (repo, module_path) tuples of dependencies from the top N modules
    dep_keys: set[tuple[str, str]] = set()
    for m in modules[:hops]:
        try:
            deps = await graph_db.get_direct_dependencies(
                m["repo"], m["module_path"],
            )
        except Exception as exc:
            log.warning("dep_expansion_failed", repo=m["repo"], error=str(exc)[:120])
            continue
        for d in deps:
            if d.get("repo") and d.get("path"):
                dep_keys.add((d["repo"], d["path"]))

    # Subtract what's already in the list
    existing = {(m["repo"], m["module_path"]) for m in modules}
    new_keys = dep_keys - existing
    if not new_keys:
        return modules

    # Fetch full rows from PostgreSQL — latest version per dep
    extra: list[dict] = []
    for repo, path in new_keys:
        row = await vs.get_module_by_path(db, repo, path)
        if row:
            row = dict(row)
            row.setdefault("similarity", 0.5)
            extra.append(row)

    if extra:
        log.info("dep_expansion", added=len(extra),
                 keys=[f"{r['repo']}/{r['module_path']}" for r in extra[:5]])
    return modules + extra


async def _multi_query_search(
    db, query_vec: list[float],
    top_k: int, repo_filter, tag_filter, version_filter,
    expand_deps: bool,
) -> list[dict]:
    """Search modules by embedding similarity, optionally expanding with deps.

    For compose/generate the shopping list (which has the full module catalog)
    handles component discovery — this function just provides the initial
    semantic matches and their dependency context.
    """
    primary = await vs.similarity_search(
        db,
        query_embedding=query_vec,
        top_k=top_k,
        repo_filter=repo_filter,
        tag_filter=tag_filter,
        version_filter=version_filter,
    )

    if not expand_deps:
        return primary

    deduped = _dedupe_modules(primary)

    # Expand with each top-N module's direct dependencies — so the
    # LLM understands what a "fargate_service" module already brings with it
    # (ALB, security group, IAM role) and doesn't try to call those modules
    # itself.
    with_deps = await _expand_with_dependencies(db, deduped, hops=7)

    return with_deps[:top_k * 3]


async def query(request: QueryRequest, db) -> QueryResponse:
    # Agent-based pipeline: delegate to agentic tool-use loop if enabled.
    # Works for all query types (compose, optimize, audit, search).
    if (
        settings.agent_compose_enabled
        and (settings.anthropic_api_key or settings.aws_bedrock_region)
    ):
        from app.services.agent import agent_query
        return await agent_query(request, db)

    t0 = time.monotonic()

    query_vec = embed_query(request.query, query_type=request.query_type)
    similar = await _multi_query_search(
        db,
        query_vec=query_vec,
        top_k=request.top_k,
        repo_filter=request.repo_filter,
        tag_filter=request.tag_filter,
        version_filter=request.version_filter,
        expand_deps=_is_compose_mode(request.query_type),
    )

    # Parallel path: search usage snippets for existing deployments
    reference_context = ""
    if settings.retriever_fetch_reference_code:
        reference_context = await _search_reference_deployments(db, query_vec, request.query)

    # Compose-pattern search (stack-level reference files; only used for generate)
    compose_context = ""
    if _is_compose_mode(request.query_type):
        compose_context = await _search_compose_patterns(db, query_vec)

    # Enrich with repo URLs and per-module latest tag for source path generation
    for r in similar:
        r["_repo_url"] = await _get_repo_url(db, r["repo"])
        r["_latest_tag"] = await _get_module_latest_tag(db, r["repo"], r["module_path"])

    sources = [
        QueryResult(
            module_name=r["module_name"],
            repo=r["repo"],
            module_path=r["module_path"],
            version=r.get("version", "latest"),
            tags=r["tags"] or [],
            similarity=round(float(r["similarity"]), 4),
            description=r["description"],
        )
        for r in similar
    ]

    # Enrich with dependencies from graph
    graph_context = ""
    if similar:
        deps = await graph_db.get_dependency_tree(similar[0]["module_path"], depth=2, repo=similar[0].get("repo"))
        if deps:
            graph_context = "\n\nDependency chains:\n" + "\n".join(
                " -> ".join(d["chain"]) for d in deps[:5]
            )

    # Fetch knowledge snippets for top modules
    snippet_context = await _get_snippet_context(db, similar, request.query)

    # Module catalog (only for generate — adds ~1.5k tokens but lets the LLM
    # see modules outside top-K)
    module_catalog = (
        await _build_module_catalog(db) if _is_compose_mode(request.query_type) else ""
    )

    answer = await _agenerate_answer(request.query, request.query_type, similar,
                                     graph_context, snippet_context,
                                     reference_context=reference_context,
                                     module_catalog=module_catalog,
                                     compose_context=compose_context)
    latency_ms = int((time.monotonic() - t0) * 1000)
    return QueryResponse(answer=answer, sources=sources, latency_ms=latency_ms)


async def stream_query(request: QueryRequest, db):
    """Streaming version of query — yields SSE events."""
    import json as _json

    # Agent-based pipeline: delegate to agentic tool-use loop if enabled.
    # Works for all query types (compose, optimize, audit, search).
    if (
        settings.agent_compose_enabled
        and (settings.anthropic_api_key or settings.aws_bedrock_region)
    ):
        from app.services.agent import agent_stream_query
        async for event in agent_stream_query(request, db):
            yield event
        return

    t0 = time.monotonic()

    query_vec = embed_query(request.query, query_type=request.query_type)
    similar = await _multi_query_search(
        db,
        query_vec=query_vec,
        top_k=request.top_k,
        repo_filter=request.repo_filter,
        tag_filter=request.tag_filter,
        version_filter=request.version_filter,
        expand_deps=_is_compose_mode(request.query_type),
    )

    # Parallel path: search usage snippets for existing deployments
    reference_context = ""
    if settings.retriever_fetch_reference_code:
        reference_context = await _search_reference_deployments(db, query_vec, request.query)

    # Compose-pattern search — stack-level reference files (generate only)
    compose_context = ""
    if _is_compose_mode(request.query_type):
        compose_context = await _search_compose_patterns(db, query_vec)

    # Two-step generation (only for generate): ask the LLM which modules to
    # use, then fetch their full details so the next LLM call gets
    # exhaustive variables/outputs instead of relying on a 200-char
    # description.
    shopping_modules: list[dict] = []
    if _is_compose_mode(request.query_type):
        picked = await _build_shopping_list(
            db, request.query, similar, compose_context,
        )
        if picked:
            shopping_modules = await _fetch_module_details(db, picked)
            # Move shopping-list modules to the FRONT of `similar`, dedupe by key
            seen = {(r["repo"], r["module_path"]) for r in shopping_modules}
            similar = shopping_modules + [
                r for r in similar
                if (r["repo"], r["module_path"]) not in seen
            ]
            log.info("two_step_applied",
                     shopping_count=len(shopping_modules),
                     total_in_context=len(similar))

    for r in similar:
        r["_repo_url"] = await _get_repo_url(db, r["repo"])
        r["_latest_tag"] = await _get_module_latest_tag(db, r["repo"], r["module_path"])

    sources = [
        {
            "module_name": r["module_name"],
            "repo": r["repo"],
            "module_path": r["module_path"],
            "version": r.get("version", ""),
            "tags": r["tags"] or [],
            "similarity": round(float(r["similarity"]), 4),
            "description": r["description"],
        }
        for r in similar
    ]

    # Send sources first
    yield f"data: {_json.dumps({'type': 'sources', 'sources': sources})}\n\n"

    # Build context
    graph_context = ""
    if similar:
        deps = await graph_db.get_dependency_tree(similar[0]["module_path"], depth=2, repo=similar[0].get("repo"))
        if deps:
            graph_context = "\n\nDependency chains:\n" + "\n".join(
                " -> ".join(d["chain"]) for d in deps[:5]
            )

    context_text = _build_context_text(similar)

    # Fetch knowledge snippets
    snippet_context = await _get_snippet_context(db, similar, request.query)

    system = SYSTEM_PROMPTS.get(request.query_type, SYSTEM_PROMPTS["search"])
    prompt = f"Query: {request.query}\n\nRelevant modules:\n\n{context_text}{graph_context}"
    if snippet_context:
        prompt += f"\n\n---\n\nUsage conventions (follow these patterns):\n{snippet_context}"
    if reference_context:
        prompt += f"\n\n---\n\n{reference_context}"
    if compose_context:
        prompt += f"\n\n---\n\n{compose_context}"
    # For generate, also inject the full organisation module catalog so the
    # LLM knows what's available outside top-K — prevents it from silently
    # generating raw aws_vpc / aws_acm_certificate / aws_lb resources when
    # modules exist for those domains.
    if _is_compose_mode(request.query_type):
        catalog = await _build_module_catalog(db)
        prompt += f"\n\n---\n\n{catalog}"

    # Stream answer tokens. Wrap the LLM iterator so that backend errors
    # (timeouts, API failures, network issues) always emit an `error` event
    # followed by `done` — otherwise the frontend's reader.read() loop never
    # observes a terminal event and the UI hangs with a spinner.
    error_message: str | None = None
    try:
        async for chunk in llm.astream(prompt, system=system, max_tokens=16384):
            yield f"data: {_json.dumps({'type': 'token', 'token': chunk})}\n\n"
    except Exception as exc:
        log.exception("stream_query_llm_error", error=f"{type(exc).__name__}: {exc}")
        error_message = "Internal error while generating response"

    latency_ms = int((time.monotonic() - t0) * 1000)
    if error_message:
        yield f"data: {_json.dumps({'type': 'error', 'message': error_message, 'latency_ms': latency_ms})}\n\n"
    yield f"data: {_json.dumps({'type': 'done', 'latency_ms': latency_ms, 'ok': error_message is None})}\n\n"


def _build_context_text(modules: list[dict]) -> str:
    """Build rich module context for LLM prompts, including variable details."""
    parts = []
    for m in modules:
        lines = [
            f"Module: {m['module_name']} (repo: {m['repo']})",
            f'Source: "{_build_module_source(m)}"',
            f"Tags: {', '.join(m['tags'] or [])}",
            f"Description: {m['description']}",
        ]

        # Variables with full detail
        variables = m.get("variables") or {}
        if variables:
            lines.append("Variables:")
            for vname, vinfo in variables.items():
                if isinstance(vinfo, dict):
                    req = "required" if vinfo.get("required") else "optional"
                    vtype = vinfo.get("type", "any")
                    desc = vinfo.get("description", "")
                    default = vinfo.get("default")
                    detail = f"  - {vname}: type={vtype}, {req}"
                    if desc:
                        detail += f', description="{desc}"'
                    if default is not None and not vinfo.get("required"):
                        detail += f", default={default}"
                    lines.append(detail)
                else:
                    lines.append(f"  - {vname}")

        # Outputs with descriptions
        outputs = m.get("outputs") or {}
        if outputs:
            lines.append("Outputs:")
            for oname, oinfo in outputs.items():
                if isinstance(oinfo, dict):
                    desc = oinfo.get("description", "")
                    detail = f"  - {oname}"
                    if desc:
                        detail += f': {desc}'
                    lines.append(detail)
                else:
                    lines.append(f"  - {oname}")

        resources = m.get("resources") or []
        if resources:
            lines.append(f"Resources: {', '.join(resources)}")

        parts.append("\n".join(lines))
    return "\n\n---\n\n".join(parts)


async def _search_compose_patterns(db, query_vec: list[float]) -> str:
    """Find stack-level patterns matching the query.

    Combines:
      - stack_pattern rows (cross-consumer aggregations — strongest signal
        when the same module-set appears in multiple consumer repos)
      - compose_pattern rows (concrete real files — secondary signal)

    For "build me a full X" queries this is the highest-value context
    because it shows the canonical organisation pattern for a complete
    stack, not just isolated modules.
    """
    # Stack patterns first (aggregated, strongest evidence)
    stack_hits = await vs.snippet_similarity_search(
        db, query_embedding=query_vec, top_k=3, kind_filter=["stack_pattern"],
    )
    stack_hits = [s for s in stack_hits if s.get("similarity", 0) >= 0.50]

    # Compose patterns (individual files)
    file_hits = await vs.snippet_similarity_search(
        db, query_embedding=query_vec, top_k=5, kind_filter=["compose_pattern"],
    )
    file_hits = [s for s in file_hits if s.get("similarity", 0) >= 0.55]

    if not stack_hits and not file_hits:
        return ""

    parts: list[str] = []

    if stack_hits:
        parts.append(
            "## Canonical stack patterns (aggregated across consumer repos)"
        )
        parts.append(
            "These are recurring module combinations observed in multiple "
            "files. **Prefer the highest-evidence combination that matches "
            "the request** — it is the organisation's canonical answer."
        )
        for s in stack_hits[:2]:
            sim_pct = int(s.get("similarity", 0) * 100)
            ev = s.get("evidence_count", 0)
            parts.append(
                f"\n### Stack pattern (similarity {sim_pct}%, used in {ev} files)\n"
                f"{s['summary']}"
            )

    if file_hits:
        # Take top 2 distinct files
        seen_files: set[str] = set()
        chosen: list[dict] = []
        for s in file_hits:
            loc = (s.get("source_locator") or "").split(":L")[0]
            if loc and loc not in seen_files:
                seen_files.add(loc)
                chosen.append(s)
            if len(chosen) >= 2:
                break
        if chosen:
            parts.append(
                "\n## Concrete stack files (replicate the wiring)"
            )
            for s in chosen:
                sim_pct = int(s.get("similarity", 0) * 100)
                parts.append(
                    f"### `{s.get('source_locator', '?')}` "
                    f"(similarity {sim_pct}%, "
                    f"{s.get('evidence_count', 0)} module calls)\n"
                    f"{s['summary']}"
                )
                related = s.get("related_refs") or []
                if related:
                    parts.append(f"Modules wired together: {', '.join(related)}")

    return "\n\n".join(parts)


async def _search_reference_deployments(
    db, query_vec: list[float], query: str
) -> str:
    """Search usage snippets directly for existing deployments matching the query.

    This is a parallel search path — independent of the module search.
    When the query mentions a known deployment (e.g. 'artemis'), snippet
    similarity search will find it even though no source module is named 'artemis'.

    Returns a formatted context section with reference HCL code, or empty string.
    """
    max_snippets = settings.retriever_max_reference_snippets
    max_lines = settings.retriever_max_reference_lines
    min_similarity = 0.70  # threshold — below this, snippets are too generic

    # Direct semantic search on usage snippets
    similar_usages = await vs.snippet_similarity_search(
        db,
        query_embedding=query_vec,
        top_k=max_snippets * 3,
        kind_filter=["usage"],
    )

    # Filter by similarity threshold and require source_locator
    relevant = [
        s for s in similar_usages
        if s.get("similarity", 0) >= min_similarity and s.get("source_locator")
    ]

    if not relevant:
        return ""

    # Deduplicate by file (multiple usage snippets can point to same file)
    seen_files: set[str] = set()
    to_fetch: list[dict] = []
    for s in relevant:
        file_key = s["source_locator"].split(":L")[0]
        if file_key not in seen_files:
            seen_files.add(file_key)
            to_fetch.append(s)
        if len(to_fetch) >= max_snippets:
            break

    # Fetch actual HCL code from git
    fragments: list[str] = []
    for usage in to_fetch:
        locator = usage["source_locator"]
        # Fetch full file (no line range) for maximum context
        file_locator = locator.split(":L")[0]
        code = await fetch_fragment(file_locator)
        if not code:
            log.debug("reference_deployment_fetch_failed", locator=file_locator)
            continue

        # Truncate if too long
        code_lines = code.splitlines()
        if len(code_lines) > max_lines:
            code = "\n".join(code_lines[:max_lines])
            code += f"\n# ... truncated ({len(code_lines) - max_lines} more lines)"

        sim_pct = int(usage.get("similarity", 0) * 100)
        module_ref = usage.get("module_ref", "")
        fragments.append(
            f"### Existing deployment: `{file_locator}` (similarity {sim_pct}%)\n"
            f"Uses: {module_ref}\n"
            f"```hcl\n{code}\n```"
        )
        log.info("reference_deployment_found",
                 locator=file_locator, similarity=sim_pct, module_ref=module_ref)

    if not fragments:
        return ""

    header = (
        "## Reference deployments (existing HCL from this organisation)\n"
        "These are real, production deployments that closely match your query. "
        "Use them as the PRIMARY structural reference — replicate module wiring, "
        "variable passing, data sources, and naming patterns. "
        "Adapt to the specific parameters in the user's request.\n\n"
    )
    return header + "\n\n".join(fragments)


async def _get_snippet_context(db, modules: list[dict], query: str) -> str:
    """Fetch convention + usage snippets for the top modules."""
    if not modules:
        return ""

    lines = []
    for m in modules[:3]:  # top 3 modules
        module_ref = f"{m['repo']}/{m['module_path']}"
        snippets = await vs.get_snippets_for_module(
            db, module_ref,
            kinds=[f"convention.{d}" for d in
                   ("naming", "vars", "codeploy", "tagging", "layout", "versions")],
        )
        if not snippets:
            continue

        fresh = [s for s in snippets if not s.get("stale")]
        stale = [s for s in snippets if s.get("stale")]

        if fresh:
            # Sort by evidence_count descending — strongest conventions first
            fresh.sort(key=lambda s: s.get("evidence_count", 0), reverse=True)
            lines.append(f"\n## Conventions for {module_ref}")
            for s in fresh:
                dim = s["kind"].replace("convention.", "")
                ev = s.get("evidence_count", 0)
                confidence = "high" if ev >= 5 else "low" if ev <= 2 else "moderate"
                lines.append(f"**{dim}** ({ev} deployments, {confidence} confidence):")
                lines.append(s["summary"])

        if stale:
            lines.append(f"\n## Conventions for {module_ref} (stale — use with caution)")
            for s in stale:
                dim = s["kind"].replace("convention.", "")
                lines.append(f"**{dim}** [STALE] ({s.get('evidence_count', 0)} deployments):")
                lines.append(s["summary"])

        # Add a few usage examples
        usages = await vs.get_snippets_for_module(
            db, module_ref, kinds=["usage"], limit=5,
        )
        if usages:
            lines.append(f"\nRecent usage examples:")
            for u in usages[:5]:
                lines.append(f"- {u['summary']}")

    return "\n".join(lines)



async def _agenerate_answer(query: str, query_type: str, context_modules: list[dict],
                            graph_context: str, snippet_context: str = "",
                            reference_context: str = "",
                            module_catalog: str = "",
                            compose_context: str = "") -> str:
    if not context_modules:
        return "No relevant modules found in the knowledge base."

    context_text = _build_context_text(context_modules)

    system = SYSTEM_PROMPTS.get(query_type, SYSTEM_PROMPTS["search"])
    prompt = f"Query: {query}\n\nRelevant modules:\n\n{context_text}{graph_context}"
    if snippet_context:
        prompt += f"\n\n---\n\nUsage conventions (follow these patterns):\n{snippet_context}"
    if reference_context:
        prompt += f"\n\n---\n\n{reference_context}"
    if compose_context:
        prompt += f"\n\n---\n\n{compose_context}"
    if module_catalog and _is_compose_mode(query_type):
        prompt += f"\n\n---\n\n{module_catalog}"

    result = await llm.acomplete(prompt, system=system, max_tokens=16384)
    if result:
        return result

    # Fallback without LLM
    lines = [f"Found {len(context_modules)} relevant module(s):"]
    for m in context_modules:
        lines.append(f"\n- {m['module_name']} ({m['repo']}): {m['description'] or ''}")
    return "\n".join(lines)
