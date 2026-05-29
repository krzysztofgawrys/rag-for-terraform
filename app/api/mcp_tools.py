"""
Terraform RAG - MCP tools mounted as ASGI sub-app inside FastAPI.

Accessible at:  POST http://localhost:8000/mcp/
Claude Code config (.mcp.json):
    { "type": "http", "url": "http://localhost:8000/mcp/" }
"""

from typing import Optional

import structlog
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.core.audit import audit_mcp_tool
from app.core.config import get_settings
from app.core.vector_store import AsyncSessionLocal
from app.core import graph as graph_db

log = structlog.get_logger("mcp_tools")


def _require_write_role() -> str | None:
    """Return an error message if the caller is readonly, else None."""
    ctx = structlog.contextvars.get_contextvars()
    role = ctx.get("user_role")
    if role == "readonly":
        return (
            "**This tool requires a 'user' or 'admin' role.** "
            "Your API key has 'readonly' access."
        )
    return None

_MCP_INSTRUCTIONS = (
    "Terraform module knowledge base for this organisation. "
    "\n\n"
    "**For building/extending infrastructure** (preferred workflow):\n"
    "1. `pick_modules(query)` - cheap Haiku call returns a tight list of "
    "canonical `repo//module_path` references for the request.\n"
    "2. `get_module_details(repo, module_path)` for each pick - full "
    "variables, outputs, conventions.\n"
    "3. Generate the HCL yourself from those details.\n"
    "\n"
    "Or use `query_modules(query, query_type='compose')` for a richer "
    "one-shot context that includes the catalog, stack patterns, "
    "compose patterns, and conventions - but no shopping list.\n"
    "\n"
    "**Browse the index:** `list_modules`, `get_module_details`.\n"
    "**Dependency graph:** `get_dependencies`.\n"
    "**Conventions / usage:** `get_module_usage`, `find_similar_usages`.\n"
    "**Raw HCL from git:** `fetch_example_code`."
)


def _create_mcp() -> FastMCP:
    """Create FastMCP instance, optionally with Cognito OAuth."""
    settings = get_settings()

    if (settings.auth_mode == "sso"
            and settings.cognito_user_pool_id
            and settings.mcp_oauth_issuer_url):
        from mcp.server.auth.settings import (
            AuthSettings, ClientRegistrationOptions, RevocationOptions,
        )
        from urllib.parse import urlparse
        from app.core.cognito_oauth import CognitoOAuthProvider

        # Behind ALB: allow the external domain in Host header
        parsed = urlparse(settings.mcp_oauth_issuer_url)
        ts = TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[parsed.netloc, f"{parsed.netloc}:*"],
            allowed_origins=[settings.mcp_oauth_issuer_url],
        )

        provider = CognitoOAuthProvider(settings)
        server = FastMCP(
            name="terraform-rag",
            stateless_http=True,
            instructions=_MCP_INSTRUCTIONS,
            auth_server_provider=provider,
            transport_security=ts,
            auth=AuthSettings(
                issuer_url=settings.mcp_oauth_issuer_url,
                resource_server_url=f"{settings.mcp_oauth_issuer_url}/mcp",
                client_registration_options=ClientRegistrationOptions(
                    enabled=False,
                    valid_scopes=["openid", "email", "profile"],
                    default_scopes=["openid", "email", "profile"],
                ),
                revocation_options=RevocationOptions(enabled=True),
            ),
        )
        provider.register_callback_route(server)
        log.info("mcp_oauth_enabled", issuer=settings.mcp_oauth_issuer_url)
        return server

    # Build allowed_hosts from frontend_url + localhost defaults
    from urllib.parse import urlparse
    allowed_hosts = ["127.0.0.1", "127.0.0.1:8000", "localhost", "localhost:8000"]
    for origin in settings.frontend_url.split(","):
        parsed = urlparse(origin.strip())
        if parsed.hostname and parsed.hostname not in ("127.0.0.1", "localhost"):
            allowed_hosts.append(parsed.netloc)
            allowed_hosts.append(parsed.hostname)

    return FastMCP(
        name="terraform-rag",
        stateless_http=True,
        instructions=_MCP_INSTRUCTIONS,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
        ),
    )


mcp = _create_mcp()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
@audit_mcp_tool
async def query_modules(
    query: str,
    query_type: str = "generate",
    repo_filter: Optional[str] = None,
    tag_filter: Optional[str] = None,
    top_k: int = 5,
) -> str:
    """
    Search the Terraform module knowledge base and return context for code generation.

    Returns module details, conventions, compose/stack patterns, the organisation
    module catalog, and reference deployment code — does NOT call the final
    generating LLM. Use this context to generate Terraform code yourself.

    For query_type in (generate, compose), runs the full RAG pipeline:
      * Multi-query expansion: the request is decomposed (by a cheap LLM)
        into 3-7 atomic sub-queries and each is searched separately.
      * Dependency expansion: 1-hop deps of the top modules are added.
      * Compose + stack patterns: real consumer files and aggregated
        canonical combinations are included.
      * Module catalog: the full grouped catalog of all indexed modules,
        with [UNUSED IN ANY DEPLOYMENT] flags and semver-tagged refs.

    For search/optimize/audit, just runs a single similarity search with
    conventions and reference deployments — no expansion or catalog.

    Args:
        query:       Natural language question or instruction.
        query_type:  One of: generate | compose | search | optimize | audit
                     - generate / compose: full pipeline (recommended for
                       building or extending infrastructure)
                     - search / optimize / audit: lightweight context
        repo_filter: Limit search to a specific repo name (optional).
        tag_filter:  Comma-separated tags to filter by (optional).
        top_k:       Base number of similar modules to retrieve (default 5).
                     For generate/compose, multi-query and dep expansion
                     may increase the total context to ~top_k * 3.
    """
    settings = get_settings()
    if settings.demo_mode:
        return (
            "**query_modules is disabled in the public demo** to control LLM costs.\n\n"
            "Use the free tools instead:\n"
            "- `list_modules` - browse or semantic-search the module catalog\n"
            "- `get_module_details` - full variables, outputs, versions\n"
            "- `get_module_usage` - conventions and usage examples\n"
            "- `get_dependencies` - dependency tree\n\n"
            "Run your own instance for full access: "
            "https://github.com/krzysztofgawrys/rag-for-terraform"
        )
    role_err = _require_write_role()
    if role_err:
        return role_err

    from app.core.embeddings import embed_query as _embed
    from app.core.vector_store import get_snippets_for_module
    from app.services.retriever import (
        _get_repo_url, _get_module_latest_tag,
        _build_module_source, _search_reference_deployments,
        _multi_query_search, _search_compose_patterns,
        _build_module_catalog, _is_compose_mode,
    )

    tags = [t.strip() for t in tag_filter.split(",") if t.strip()] if tag_filter else None

    async with AsyncSessionLocal() as db:
        from app.core import vector_store as _vs

        query_vec = _embed(query, query_type=query_type)

        # 1. Module search — full pipeline for generate/compose, single
        # query for search/optimize/audit.
        similar = await _multi_query_search(
            db,
            query_vec=query_vec,
            top_k=top_k,
            repo_filter=repo_filter,
            tag_filter=tags,
            version_filter=None,
            expand_deps=_is_compose_mode(query_type),
        )

        # Enrich with source paths — per-module latest tag (NOT per-repo)
        for r in similar:
            r["_repo_url"] = await _get_repo_url(db, r["repo"])
            r["_latest_tag"] = await _get_module_latest_tag(
                db, r["repo"], r["module_path"]
            )

        # 2. Reference deployments (parallel search on usage snippets)
        reference_context = await _search_reference_deployments(db, query_vec, query)

        # 3. Compose + stack patterns (only for generate/compose)
        compose_context = ""
        if _is_compose_mode(query_type):
            compose_context = await _search_compose_patterns(db, query_vec)

        # 4. Module catalog (only for generate/compose)
        module_catalog = ""
        if _is_compose_mode(query_type):
            module_catalog = await _build_module_catalog(db)

        # 5. Conventions for top modules — up to 5 modules, full 6 dimensions
        convention_lines = []
        n_conv_modules = 5 if _is_compose_mode(query_type) else 3
        for m in similar[:n_conv_modules]:
            module_ref = f"{m['repo']}/{m['module_path']}"
            snippets = await get_snippets_for_module(
                db, module_ref,
                kinds=[f"convention.{d}" for d in
                       ("naming", "vars", "codeploy", "tagging", "layout", "versions")],
            )
            fresh = [s for s in snippets if not s.get("stale")]
            if fresh:
                fresh.sort(key=lambda s: s.get("evidence_count", 0), reverse=True)
                convention_lines.append(f"\n### Conventions for {module_ref}")
                # All dimensions for compose mode, top 3 otherwise
                max_dims = 6 if _is_compose_mode(query_type) else 3
                for s in fresh[:max_dims]:
                    dim = s["kind"].replace("convention.", "")
                    ev = s.get("evidence_count", 0)
                    convention_lines.append(
                        f"**{dim}** ({ev} deployments): {s['summary'][:300]}"
                    )

    # Format output
    lines = [f"**{len(similar)} module(s) found**\n"]

    for m in similar:
        source = _build_module_source(m)
        variables = m.get("variables") or {}
        outputs = m.get("outputs") or {}
        req_vars = [v for v, d in variables.items()
                    if isinstance(d, dict) and d.get("required")]
        opt_vars = [v for v, d in variables.items()
                    if isinstance(d, dict) and not d.get("required")]
        output_names = list(outputs.keys())

        lines.append(
            f"### {m['module_name']} (`{m['repo']}//{m['module_path']}`)\n"
            f"Source: `{source}`\n"
            f"Tags: {', '.join(m.get('tags') or [])}\n"
            f"Description: {m.get('description', '')[:200]}\n"
            f"Required vars: {', '.join(req_vars) or '—'}\n"
            f"Optional vars: {', '.join(opt_vars[:10]) or '—'}"
            f"{'...' if len(opt_vars) > 10 else ''}\n"
            f"Outputs: {', '.join(output_names) or '—'}\n"
        )

    if convention_lines:
        lines.append("\n---\n## Conventions")
        lines.extend(convention_lines)

    if reference_context:
        lines.append(f"\n---\n{reference_context}")

    if compose_context:
        lines.append(f"\n---\n{compose_context}")

    if module_catalog:
        lines.append(f"\n---\n{module_catalog}")

    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def pick_modules(query: str, top_k: int = 8) -> str:
    """
    Ask a cheap LLM (Haiku) which specific modules should be used to build
    the requested infrastructure. Returns a curated shopping list of
    `repo//module_path` references.

    This is the FIRST step of a two-step generation workflow:
      1. `pick_modules(query)` → list of chosen modules
      2. `get_module_details(repo, module_path)` for each pick →
         full variables/outputs/conventions
      3. Generate the HCL yourself using those details.

    The picker LLM sees:
      - the user query
      - the organisation module catalog (all 100+ modules with
        descriptions and [UNUSED] flags)
      - the top semantic matches
      - any matching compose/stack patterns

    Compared to `query_modules`, this:
      - Costs ~10x less (one small Haiku call instead of the full pipeline)
      - Returns a tight, opinionated module list ready for HCL generation
      - Is the right tool when you already know roughly what you want
        and need help mapping the request to canonical org modules.

    Args:
        query:  Natural language description of what to build.
        top_k:  Number of semantic matches to feed the picker (default 8).

    Returns:
        Markdown listing the chosen modules (one per line, with full
        `repo//module_path?ref=<tag>` source paths) plus a short note
        on next steps.
    """
    settings = get_settings()
    if settings.demo_mode:
        return (
            "**pick_modules is disabled in the public demo** to control LLM costs.\n\n"
            "Use the free tools instead:\n"
            "- `list_modules(semantic_query='...')` - semantic search across modules\n"
            "- `get_module_details` - full variables, outputs, versions\n"
            "- `get_module_usage` - conventions and usage examples\n\n"
            "Run your own instance for full access: "
            "https://github.com/krzysztofgawrys/rag-for-terraform"
        )
    role_err = _require_write_role()
    if role_err:
        return role_err

    from app.core.embeddings import embed_query as _embed
    from app.services.retriever import (
        _multi_query_search, _build_module_catalog, _search_compose_patterns,
        _build_shopping_list, _fetch_module_details,
        _get_module_latest_tag, _get_repo_url, _build_module_source,
    )
    from app.core import vector_store as _vs

    async with AsyncSessionLocal() as db:
        query_vec = _embed(query, query_type="generate")

        # 1. Multi-query semantic search (with sub-query expansion)
        similar = await _multi_query_search(
            db,
            query_vec=query_vec,
            top_k=top_k,
            repo_filter=None,
            tag_filter=None,
            version_filter=None,
            expand_deps=True,
        )

        # 2. Compose/stack-pattern context (helps the picker prefer canonical
        #    combinations the org has used before)
        compose_context = await _search_compose_patterns(db, query_vec)

        # 3. The actual shopping-list LLM call
        picks = await _build_shopping_list(
            db, query, similar, compose_context,
        )
        if not picks:
            return (
                "**No modules picked.** The shopping-list LLM returned an "
                "empty result. Try `query_modules(query, query_type='compose')` "
                "for the full context, or refine your query."
            )

        # 5. Resolve each pick to a full source URL (?ref=<latest-tag>)
        rows = await _fetch_module_details(db, picks)

    lines = [f"**{len(picks)} module(s) picked for: _{query[:80]}{'...' if len(query) > 80 else ''}_**\n"]
    for repo, module_path in picks:
        # Find the matching row if present
        match = next(
            (r for r in rows
             if r.get("repo") == repo and r.get("module_path") == module_path),
            None,
        )
        if match:
            match = dict(match)
            match["_repo_url"] = await _get_repo_url(db, repo) if False else match.get("_repo_url", "")
            # _fetch_module_details sets similarity=0.95, but we re-look up
            # source via _build_module_source so we need _repo_url + _latest_tag.
            async with AsyncSessionLocal() as db2:
                match["_repo_url"] = await _get_repo_url(db2, repo)
                match["_latest_tag"] = await _get_module_latest_tag(
                    db2, repo, module_path,
                )
            source = _build_module_source(match)
            desc = (match.get("description") or "").split("\n", 1)[0][:120]
            lines.append(f"- `{source}`")
            if desc:
                lines.append(f"  _{desc}_")
        else:
            lines.append(f"- `{repo}//{module_path}` _(picked but not found in module index — verify the path)_")

    lines.append(
        "\n**Next steps:** call `get_module_details(repo, module_path)` for "
        "each pick to fetch full variables, outputs, and conventions before "
        "composing the HCL."
    )
    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def list_modules(
    repo: Optional[str] = None,
    tag: Optional[str] = None,
    resource_type: Optional[str] = None,
    search: Optional[str] = None,
    semantic_query: Optional[str] = None,
    limit: int = 30,
) -> str:
    """
    List indexed Terraform modules (one entry per unique module, latest version).

    Two search modes:
    - **substring** (default): use `search` for case-insensitive name/path match.
    - **semantic**: use `semantic_query` for natural language similarity search
      (e.g. "VPC with private subnets", "ECS Fargate service with ALB").
      Results are ranked by cosine similarity. Filters (repo, tag, resource_type)
      still apply on top of semantic results.

    Args:
        repo:           Filter by repository name (e.g. my-terraform-modules).
        tag:            Filter by tag (e.g. s3, networking, prod).
        resource_type:  Filter by AWS resource type (e.g. aws_s3_bucket).
        search:         Case-insensitive substring match on module name or path.
        semantic_query: Natural language query for semantic similarity search.
        limit:          Max results (default 30).
    """
    from sqlalchemy import text

    # --- Semantic search path ---
    if semantic_query:
        from app.core.embeddings import embed_query as _embed
        from app.core.vector_store import similarity_search

        query_vec = _embed(semantic_query, query_type="search")
        async with AsyncSessionLocal() as db:
            results = await similarity_search(
                db,
                query_embedding=query_vec,
                top_k=limit,
                repo_filter=repo,
                tag_filter=tag,
                version_filter=None,
            )

        # Apply resource_type filter post-search (not supported by similarity_search)
        if resource_type:
            results = [r for r in results if resource_type in (r.get("resources") or [])]

        if not results:
            return "No modules found matching the semantic query."

        lines = [f"**{len(results)} module(s)** (semantic search: \"{semantic_query}\")\n"]
        for m in results:
            tags_str = ", ".join(m.get("tags") or [])
            resources_str = ", ".join((m.get("resources") or [])[:4])
            sim = int(m.get("similarity", 0) * 100)
            lic = m.get("license") or "Unknown"
            lines.append(
                f"### {m['module_name']} (similarity {sim}%)\n"
                f"- Repo: `{m['repo']}`  Path: `{m['module_path']}`  Version: `{m.get('version', '?')}`\n"
                f"- License: {lic}\n"
                f"- Tags: {tags_str or '—'}\n"
                f"- Resources: {resources_str or '—'}\n"
                f"- Description: {(m.get('description') or '—')[:120]}\n"
            )
        return "\n".join(lines)

    # --- Substring / filter search path ---
    conditions = ["TRUE"]
    params: dict = {"limit": limit}
    if repo:
        conditions.append("repo = :repo")
        params["repo"] = repo
    if tag:
        conditions.append(":tag = ANY(tags)")
        params["tag"] = tag
    if resource_type:
        conditions.append(":resource_type = ANY(resources)")
        params["resource_type"] = resource_type
    if search:
        conditions.append("(LOWER(module_name) LIKE :search OR LOWER(module_path) LIKE :search)")
        params["search"] = f"%{search.lower()}%"

    where = " AND ".join(conditions)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(f"""
                SELECT DISTINCT ON (repo, module_path)
                       repo, module_name, module_path, version, tags, resources,
                       description, license
                FROM modules
                WHERE {where}
                ORDER BY repo, module_path, indexed_at DESC
                LIMIT :limit
            """),
            params,
        )
        modules = [dict(r) for r in result.mappings().all()]

    if not modules:
        return "No modules found matching the given filters."

    lines = [f"**{len(modules)} module(s)**\n"]
    for m in modules:
        tags_str = ", ".join(m.get("tags") or [])
        resources_str = ", ".join((m.get("resources") or [])[:4])
        lic = m.get("license") or "Unknown"
        lines.append(
            f"### {m['module_name']}\n"
            f"- Repo: `{m['repo']}`  Path: `{m['module_path']}`  Version: `{m.get('version', '?')}`\n"
            f"- License: {lic}\n"
            f"- Tags: {tags_str or '—'}\n"
            f"- Resources: {resources_str or '—'}\n"
            f"- Description: {(m.get('description') or '—')[:120]}\n"
        )
    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def get_module_details(repo: str, module_path: str) -> str:
    """
    Get full details for a specific module: description, all versions, variables, outputs, resources.

    Args:
        repo:        Repository name (e.g. my-terraform-modules).
        module_path: Path relative to repo root (e.g. modules/s3/basic).
    """
    from sqlalchemy import text
    from app.core.vector_store import get_module_versions

    async with AsyncSessionLocal() as db:
        # Pick the latest semver version — NOT indexed_at (indexing order
        # is reversed: oldest tags are indexed last, so indexed_at DESC
        # returns v1.0.0 instead of v2.0.1).
        # Extract (major, minor, patch) from version strings like
        # "artemis-2.0.1", "v1.0.0", "1.0.2" and sort descending.
        # Branch refs (master/main/develop) are pushed to the bottom.
        result = await db.execute(
            text("""
                SELECT repo, module_name, module_path, version, tags,
                       variables, outputs, resources, description, license
                FROM modules
                WHERE repo = :repo AND module_path = :module_path
                ORDER BY
                    CASE WHEN version ~ '^(master|main|develop|HEAD)$'
                         THEN 1 ELSE 0 END,
                    (regexp_match(version, '(\\d+)\\.(\\d+)(?:\\.(\\d+))?'))[1]::int DESC NULLS LAST,
                    (regexp_match(version, '(\\d+)\\.(\\d+)(?:\\.(\\d+))?'))[2]::int DESC NULLS LAST,
                    COALESCE((regexp_match(version, '(\\d+)\\.(\\d+)(?:\\.(\\d+))?'))[3]::int, 0) DESC
                LIMIT 1
            """),
            {"repo": repo, "module_path": module_path},
        )
        row = result.mappings().first()
        if not row:
            return f"No module found: {repo}//{module_path}"
        m = dict(row)
        versions_raw = await get_module_versions(db, repo, module_path)

    version_names = [v["version"] for v in versions_raw]
    variables = m.get("variables") or {}
    outputs = m.get("outputs") or {}
    resources = m.get("resources") or []

    # Pick latest version using semver-aware sort, NOT indexed_at order.
    # versions_raw is already sorted newest-first by get_module_versions.
    # Skip branch refs (master/main/develop) when reporting "latest".
    latest_pinned = next(
        (v for v in version_names
         if v not in ("master", "main", "develop", "HEAD")),
        m.get("version", "?"),
    )

    # Build the ready-to-use Terraform source URL
    from app.services.retriever import _get_repo_url, _build_module_source
    async with AsyncSessionLocal() as db2:
        repo_url = await _get_repo_url(db2, repo)
    source_url = _build_module_source({
        "_repo_url": repo_url,
        "module_path": module_path,
        "_latest_tag": latest_pinned,
        "repo": repo,
    })

    lic = m.get("license") or "Unknown"
    lines = [
        f"## {m['module_name']}",
        "",
        f"**Repo:** `{m['repo']}`",
        f"**Path:** `{m['module_path']}`",
        f"**Latest version:** `{latest_pinned}`",
        f"**Source:** `{source_url}`",
        f"**License:** {lic}",
        f"**Tags:** {', '.join(m.get('tags') or []) or '—'}",
        "",
        f"### Description",
        m.get("description") or "—",
        "",
        f"### Versions ({len(version_names)})",
        ", ".join(version_names) if version_names else "—",
        "",
        f"### Variables ({len(variables)})",
    ]
    for var_name, var_def in variables.items():
        if isinstance(var_def, dict):
            desc = var_def.get("description", "")
            default = var_def.get("default", "—")
            vtype = var_def.get("type", "")
            lines.append(f"- `{var_name}` ({vtype}): {desc or '—'}  default={default}")
        else:
            lines.append(f"- `{var_name}`")

    lines += ["", f"### Outputs ({len(outputs)})"]
    for out_name, out_def in outputs.items():
        desc = out_def.get("description", "") if isinstance(out_def, dict) else ""
        lines.append(f"- `{out_name}`" + (f": {desc}" if desc else ""))

    lines += ["", f"### Resources ({len(resources)})"]
    lines.append(", ".join(resources[:30]) or "—")

    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def get_dependencies(repo: str, module_path: str, depth: int = 3) -> str:
    """
    Get the dependency tree and reverse dependents for a Terraform module.

    Args:
        repo:        Repository name.
        module_path: Module path relative to repo root.
        depth:       Dependency tree depth (default 3).
    """
    tree = await graph_db.get_dependency_tree(module_path, depth=depth, repo=repo)
    dependents = await graph_db.find_dependents(module_path, repo)

    lines = [f"## Dependencies for `{repo}//{module_path}`", ""]

    if tree:
        lines.append(f"### Dependency tree (depth {depth})")
        for entry in tree:
            chain = entry.get("chain", [])
            lines.append("  →  ".join(chain))
    else:
        lines.append("_No outbound dependencies found._")

    lines.append("")

    if dependents:
        lines.append(f"### Used by ({len(dependents)} module(s))")
        for d in dependents[:20]:
            lines.append(f"- `{d.get('repo', '?')}//{d.get('path', '?')}` ver={d.get('version', '?')}")
    else:
        lines.append("_No modules depend on this one._")

    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def get_module_usage(
    module_ref: str,
    top_k: int = 10,
) -> str:
    """
    Get usage examples and conventions for a specific module.

    Returns convention snippets (naming, vars, codeploy, etc.) and recent usage
    observations from consumer repos. Use this to understand HOW a module is
    typically used before generating new Terraform code.

    Args:
        module_ref: Module reference (e.g. my-modules-repo/vpc).
        top_k:      Max usage examples to return (default 10).
    """
    from app.core.vector_store import get_snippets_for_module

    async with AsyncSessionLocal() as db:
        # Get conventions first
        conventions = await get_snippets_for_module(
            db, module_ref,
            kinds=[f"convention.{d}" for d in
                   ("naming", "vars", "codeploy", "tagging", "layout", "versions")],
        )
        # Then usage examples
        usages = await get_snippets_for_module(
            db, module_ref,
            kinds=["usage"],
            limit=top_k,
        )

    lines = [f"## Usage knowledge for `{module_ref}`\n"]

    if conventions:
        fresh = [c for c in conventions if not c.get("stale")]
        fresh.sort(key=lambda c: c.get("evidence_count", 0), reverse=True)
        stale = [c for c in conventions if c.get("stale")]

        if fresh:
            lines.append("### Conventions\n")
            for c in fresh:
                dim = c["kind"].replace("convention.", "")
                ev = c.get("evidence_count", 0)
                confidence = "high" if ev >= 5 else "low" if ev <= 2 else "moderate"
                lines.append(f"**{dim}** ({ev} deployments, {confidence} confidence)")
                lines.append(c["summary"])
                lines.append("")
        if stale:
            lines.append("### Conventions (stale — use with caution)\n")
            for c in stale:
                dim = c["kind"].replace("convention.", "")
                lines.append(f"**{dim}** [STALE] ({c.get('evidence_count', 0)} deployments)")
                lines.append(c["summary"])
                lines.append("")
    else:
        lines.append("_No conventions extracted yet._\n")

    if usages:
        lines.append(f"### Usage examples ({len(usages)} most recent)\n")
        for u in usages[:top_k]:
            lines.append(f"- {u['summary']}")
    else:
        lines.append("_No usage data indexed yet._")

    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def find_similar_usages(
    query: str,
    top_k: int = 10,
    module_ref: Optional[str] = None,
) -> str:
    """
    Semantic search across all usage and convention knowledge snippets.

    Use this to find how modules are used in practice, or to find convention
    patterns similar to a natural language query.

    Args:
        query:      Natural language query (e.g. "VPC setup for production").
        top_k:      Max results (default 10).
        module_ref: Optional filter to a specific module.
    """
    from app.core.embeddings import embed_query as _embed
    from app.core.vector_store import snippet_similarity_search

    query_vec = _embed(query, query_type="search")

    async with AsyncSessionLocal() as db:
        results = await snippet_similarity_search(
            db,
            query_embedding=query_vec,
            top_k=top_k,
            module_ref_filter=module_ref,
        )

    if not results:
        return "No matching usage or convention snippets found."

    lines = [f"**{len(results)} matching snippet(s)**\n"]
    for r in results:
        sim = f"{r['similarity'] * 100:.0f}%"
        ev = r.get("evidence_count", 0)
        confidence = "high" if ev >= 5 else "low" if ev <= 2 else "moderate"
        lines.append(
            f"### [{r['kind']}] {r['module_ref']}  (sim={sim}, {ev} deployments, {confidence} confidence)\n"
            f"{r['summary']}\n"
        )

    return "\n".join(lines)


@mcp.tool()
@audit_mcp_tool
async def fetch_example_code(source_locator: str) -> str:
    """
    Fetch a code fragment from git by source_locator.

    This is the ONLY tool that returns raw HCL code — fetched on-demand from
    git, cached in Redis (5 min), never stored in PostgreSQL.

    Args:
        source_locator: Locator string (e.g. 'tf-infra-prod@abc123:eu-west-1/blaise/main.tf:L1-L45').
    """
    from app.services.git_fetcher import fetch_fragment
    from sqlalchemy import text as sa_text

    fragment = await fetch_fragment(source_locator)
    if not fragment:
        return f"Could not fetch code fragment from `{source_locator}`. The repo may not be in the local cache."

    # Look up license for the source repo
    repo_name = source_locator.split("@")[0] if "@" in source_locator else source_locator.split(":")[0]
    lic = None
    try:
        async with AsyncSessionLocal() as db:
            row = await db.execute(
                sa_text("SELECT license FROM modules WHERE repo = :repo AND license IS NOT NULL LIMIT 1"),
                {"repo": repo_name},
            )
            lic = row.scalar_one_or_none()
    except Exception:
        pass

    # Build attribution header
    attr_lines = [f"# Source: {source_locator}"]
    if lic:
        attr_lines.append(f"# License: {lic}")
    else:
        attr_lines.append("# License: Unknown - check source repository before reuse")
    attribution = "\n".join(attr_lines)

    return f"```hcl\n{attribution}\n\n{fragment}\n```"


@mcp.tool()
@audit_mcp_tool
async def get_stats() -> str:
    """
    Return statistics about the indexed Terraform knowledge base:
    total modules, repos, versions, unique tags, and top resource types.
    """
    from sqlalchemy import text

    async with AsyncSessionLocal() as db:
        row = dict((await db.execute(text("""
            SELECT
                COUNT(DISTINCT (repo, module_path)) AS total_modules,
                COUNT(DISTINCT repo)                AS total_repos,
                MAX(indexed_at)                     AS last_indexed
            FROM modules
        """))).mappings().first())

        row["unique_tags"] = (await db.execute(
            text("SELECT COUNT(DISTINCT tag) FROM modules, UNNEST(tags) AS tag")
        )).scalar() or 0

        row["unique_resource_types"] = (await db.execute(
            text("SELECT COUNT(DISTINCT rt) FROM modules, UNNEST(resources) AS rt")
        )).scalar() or 0

        row["total_versions"] = (await db.execute(
            text("SELECT COUNT(DISTINCT version) FROM modules")
        )).scalar() or 0

        top_tags = [
            {"tag": r["tag"], "count": r["cnt"]}
            for r in (await db.execute(text("""
                SELECT tag, COUNT(*) AS cnt
                FROM modules, UNNEST(tags) AS tag
                GROUP BY tag ORDER BY cnt DESC LIMIT 8
            """))).mappings().all()
        ]
        top_res = [
            {"resource": r["rt"], "count": r["cnt"]}
            for r in (await db.execute(text("""
                SELECT rt, COUNT(*) AS cnt
                FROM modules, UNNEST(resources) AS rt
                GROUP BY rt ORDER BY cnt DESC LIMIT 8
            """))).mappings().all()
        ]

        # Knowledge snippets stats
        snippet_stats = dict((await db.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE kind = 'usage') AS usage_snippets,
                COUNT(*) FILTER (WHERE kind LIKE 'convention.%%') AS convention_snippets,
                COUNT(DISTINCT module_ref) FILTER (WHERE kind = 'usage') AS modules_with_usage,
                COUNT(DISTINCT consumer_repo) FILTER (WHERE kind = 'usage') AS consumer_repos
            FROM knowledge_snippets
        """))).mappings().first())

    top_tags_str = ", ".join(f"{t['tag']}({t['count']})" for t in top_tags)
    top_res_str = ", ".join(f"{r['resource']}({r['count']})" for r in top_res)

    return (
        f"**Terraform RAG — Knowledge Base Stats**\n\n"
        f"- Unique modules:   {row.get('total_modules', '?')}\n"
        f"- Repositories:     {row.get('total_repos', '?')}\n"
        f"- Indexed versions: {row.get('total_versions', '?')}\n"
        f"- Unique tags:      {row.get('unique_tags', '?')}\n"
        f"- Resource types:   {row.get('unique_resource_types', '?')}\n"
        f"- Last indexed:     {row.get('last_indexed', '?')}\n\n"
        f"**Usage knowledge:**\n"
        f"- Usage snippets:      {snippet_stats.get('usage_snippets', 0)}\n"
        f"- Convention snippets: {snippet_stats.get('convention_snippets', 0)}\n"
        f"- Modules with usage:  {snippet_stats.get('modules_with_usage', 0)}\n"
        f"- Consumer repos:      {snippet_stats.get('consumer_repos', 0)}\n\n"
        f"**Top tags:** {top_tags_str}\n\n"
        f"**Top resources:** {top_res_str}"
    )
