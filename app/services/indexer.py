import asyncio
import json
import re
import shutil
import structlog
from pathlib import Path
from datetime import datetime, timezone

import git

from app.core.config import get_settings
from app.core.parser import parse_repository, ParsedModule
from app.core.embeddings import embed_module
from app.core import llm
from app.prompts import load_prompt
import hashlib
from app.core.vector_store import upsert_module as vs_upsert, update_index_job, get_module_by_path, find_by_code_hash
from app.core import graph as graph_db
from app.core.vector_store import make_session_factory

log = structlog.get_logger()
settings = get_settings()

_STATS_FLUSH_INTERVAL = 3  # seconds between incremental stats writes


class JobCancelled(Exception):
    """Raised when a job has been cancelled by the user."""
    pass


async def _flush_stats(job_id, stats, _last_flush: dict, table: str = "index_jobs"):
    """Write current stats and check for cancellation.

    Uses its own DB session to avoid 'another operation is in progress'
    errors on the shared session used by upsert/graph operations.
    Raises JobCancelled if the job status was set to 'cancelled'.
    """
    import time
    now = time.monotonic()
    if now - _last_flush.get("t", 0) < _STATS_FLUSH_INTERVAL:
        return
    _last_flush["t"] = now
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as flush_db:
            if table == "index_jobs":
                await update_index_job(flush_db, job_id, stats=json.dumps(stats))
            else:
                from app.core.vector_store import update_consumer_index_job
                await update_consumer_index_job(flush_db, job_id, stats=json.dumps(stats))
            # Check cancellation
            from sqlalchemy import text
            row = await flush_db.execute(
                text(f"SELECT status FROM {table} WHERE id = :id"),
                {"id": job_id},
            )
            status = row.scalar_one_or_none()
            if status == "cancelled" or status is None:
                raise JobCancelled(f"Job {job_id} cancelled by user")
    finally:
        await engine.dispose()


# -- Single-version indexing --------------------------------------------------

async def run_indexing(
    repo_url: str,
    branch: str = "main",
    commit_sha: str | None = None,
    job_id=None,
    version: str = "latest",
    checkout_ref: str | None = None,
    module_filter: list[str] | None = None,
    _manage_job_status: bool = True,
    force: bool = False,
) -> dict:
    """Index modules from a repo.

    module_filter: if set, only index modules whose module_path is in this list.
    _manage_job_status: if False, don't update job status (used by run_tag_indexing
                        which manages job status itself).
    force: if True, re-index all modules even if already indexed (regenerate
           descriptions and embeddings). Use after changing embedding model or
           description prompt.
    """
    stats = {"added": 0, "updated": 0, "failed": 0, "total": 0}
    _last_flush: dict = {}
    repo_name = _repo_name_from_url(repo_url)

    task_engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            if job_id and _manage_job_status:
                await update_index_job(db, job_id, status="running",
                                       started_at=datetime.now(timezone.utc),
                                       finished_at=None, error=None)
            try:
                repo_dir = _clone_or_pull(repo_url, branch, checkout_ref=checkout_ref)
                modules = parse_repository(repo_dir, repo_name,
                                           module_paths=module_filter)

                stats["total"] = len(modules)
                log.info("modules_found", repo=repo_name, version=version,
                         count=len(modules), filtered=module_filter is not None)

                for module in modules:
                    module.version = version

                # Skip modules already indexed for this version
                if not force:
                    new_modules = []
                    for module in modules:
                        existing = await get_module_by_path(db, module.repo, module.module_path, version)
                        if existing:
                            stats["added"] += 1
                        else:
                            new_modules.append(module)
                    if len(new_modules) < len(modules):
                        log.info("skipped_existing", repo=repo_name, version=version,
                                 new=len(new_modules), skipped=len(modules) - len(new_modules))
                    modules = new_modules
                else:
                    log.info("force_reindex", repo=repo_name, version=version,
                             count=len(modules))

                # Index new modules — try code_hash reuse before LLM
                # Include embedding model in hash so model change invalidates cache
                reused = 0
                need_llm = []
                for module in modules:
                    code_hash = hashlib.md5(
                        f"{settings.embedding_model}:{module.raw_code}".encode()
                    ).hexdigest()
                    cached = await find_by_code_hash(db, module.repo, module.module_path, code_hash)
                    if cached:
                        try:
                            # Reuse description + embedding from same code in another version
                            # embedding_str is already in pgvector text format
                            await _upsert_with_raw_embedding(
                                db, module, cached["embedding_str"],
                                cached["description"], commit_sha,
                                job_id=str(job_id) if job_id else None,
                                code_hash=code_hash)
                            await graph_db.upsert_module(module, db=db)
                            stats["added"] += 1
                            reused += 1
                        except Exception as e:
                            need_llm.append((module, code_hash))
                    else:
                        need_llm.append((module, code_hash))

                if reused:
                    log.info("code_hash_reused", repo=repo_name, version=version,
                             reused=reused, need_llm=len(need_llm))
                    if job_id and _manage_job_status:
                        await _flush_stats(job_id, stats, _last_flush)

                # LLM for modules with new/changed code
                concurrent = settings.llm_concurrent_prompts
                if concurrent > 1 and need_llm:
                    async def _describe(m: ParsedModule) -> tuple[ParsedModule, str]:
                        return m, await _agenerate_description(m)

                    for i in range(0, len(need_llm), concurrent):
                        batch = need_llm[i:i + concurrent]
                        results = await asyncio.gather(
                            *[_describe(m) for m, _ in batch],
                            return_exceptions=True,
                        )
                        for j, result in enumerate(results):
                            if isinstance(result, Exception):
                                stats["failed"] += 1
                                log.error("module_describe_failed", error=str(result))
                                continue
                            m, description = result
                            code_hash = batch[j][1]
                            try:
                                embedding = embed_module(m, description)
                                await vs_upsert(db, m, embedding, description, commit_sha,
                                                job_id=str(job_id) if job_id else None,
                                                code_hash=code_hash)
                                await graph_db.upsert_module(m, db=db)
                                stats["added"] += 1
                            except Exception as e:
                                stats["failed"] += 1
                                log.error("module_index_failed", module=m.module_name, error=str(e))
                        if job_id and _manage_job_status:
                            await _flush_stats(job_id, stats, _last_flush)
                elif need_llm:
                    for module, code_hash in need_llm:
                        try:
                            description = _generate_description(module)
                            embedding = embed_module(module, description)
                            await vs_upsert(db, module, embedding, description, commit_sha,
                                            job_id=str(job_id) if job_id else None,
                                            code_hash=code_hash)
                            await graph_db.upsert_module(module, db=db)
                            stats["added"] += 1
                        except Exception as e:
                            stats["failed"] += 1
                            log.error("module_index_failed", module=module.module_name, error=str(e))
                        if job_id and _manage_job_status:
                            await _flush_stats(job_id, stats, _last_flush)

                if job_id and _manage_job_status:
                    await update_index_job(
                        db, job_id, status="done",
                        finished_at=datetime.now(timezone.utc),
                        stats=json.dumps(stats),
                        error=None,
                    )

            except JobCancelled:
                log.info("indexing_cancelled", repo=repo_url, job_id=job_id, stats=stats)
                # Status already set to 'cancelled' by the API — just stop.
            except Exception as e:
                log.error("indexing_failed", repo=repo_url, error=str(e))
                if job_id and _manage_job_status:
                    await update_index_job(
                        db, job_id, status="failed",
                        finished_at=datetime.now(timezone.utc),
                        error=str(e),
                    )
                raise
    finally:
        await task_engine.dispose()

    return stats


# -- Multi-version indexing (git tags) ----------------------------------------

async def run_tag_indexing(
    repo_url: str,
    branch: str = "main",
    job_id=None,
    force: bool = False,
) -> dict:
    """Index HEAD as the branch name + all discovered git tags.

    Smart tag matching: if a tag has the format '<module-name>-<version>'
    (e.g. 'acm-1.0.0', 'redis-1.1.0'), only that specific module is indexed
    for the tag. Tags without a module prefix (e.g. 'v1.40') index the full repo.
    """
    repo_name = _repo_name_from_url(repo_url)

    # Clone/pull to get all refs
    repo_dir = _clone_or_pull(repo_url, branch)
    tags = _discover_tags(repo_dir)
    log.info("discovered_tags", repo=repo_name, tags=tags, count=len(tags))

    # Mark job as running (don't pass job_id to inner run_indexing calls
    # to prevent them from prematurely marking the job as done)
    if job_id:
        task_engine_init, SessionLocal_init = make_session_factory()
        try:
            async with SessionLocal_init() as db:
                await update_index_job(db, job_id, status="running",
                                       started_at=datetime.now(timezone.utc),
                                       finished_at=None, error=None)
        finally:
            await task_engine_init.dispose()

    # Parse HEAD once to build module name → path lookup
    head_modules = parse_repository(repo_dir, repo_name)
    module_lookup = _build_module_lookup(head_modules)
    log.info("module_lookup_built", repo=repo_name,
             entries=len(module_lookup))

    # Index HEAD as the branch name (e.g. "main", "master")
    # Pass job_id for module tracking but not for job status management
    combined = await run_indexing(repo_url, branch, version=branch,
                                  job_id=job_id, _manage_job_status=False,
                                  force=force)

    # Index each tag
    _tag_flush: dict = {}
    skipped_tags: list[str] = []
    for tag in tags:
        try:
            matched_paths = _match_tag_to_modules(tag, module_lookup)
            if matched_paths is None:
                log.info("indexing_tag", repo=repo_name, tag=tag,
                         matched_modules="ALL (no module prefix)")
            elif matched_paths == []:
                # Tag has a module-name prefix but matches no known module
                # → skip entirely (do NOT index every module against it).
                log.info("indexing_tag_skipped", repo=repo_name, tag=tag,
                         reason="prefix matches no known module")
                skipped_tags.append(tag)
                continue
            else:
                log.info("indexing_tag", repo=repo_name, tag=tag,
                         matched_modules=matched_paths)

            tag_stats = await run_indexing(
                repo_url, branch,
                version=tag,
                checkout_ref=tag,
                module_filter=matched_paths,
                job_id=job_id,
                _manage_job_status=False,
                force=force,
            )
            combined["added"] += tag_stats["added"]
            combined["total"] += tag_stats["total"]
            combined["failed"] += tag_stats["failed"]
        except Exception as e:
            log.error("tag_index_failed", repo=repo_name, tag=tag, error=str(e))
            combined["failed"] += 1

        # Incremental stats so the UI shows progress during tag indexing
        if job_id:
            combined["modules"] = len(head_modules)
            combined["versions"] = len(tags) + 1
            await _flush_stats(job_id, combined, _tag_flush)

    if skipped_tags:
        log.info("tags_skipped_summary", repo=repo_name,
                 count=len(skipped_tags), tags=skipped_tags)
        combined["skipped_tags"] = skipped_tags

    # Enrich stats with readable summary
    combined["modules"] = len(head_modules)
    combined["versions"] = len(tags) + 1  # tags + branch

    # Update job stats with combined totals (skip if cancelled)
    if job_id:
        task_engine, SessionLocal = make_session_factory()
        try:
            async with SessionLocal() as db:
                current = (await db.execute(
                    __import__("sqlalchemy").text("SELECT status FROM index_jobs WHERE id = :id"),
                    {"id": job_id},
                )).scalar_one_or_none()
                if current == "cancelled":
                    log.info("indexing_cancelled", repo=repo_name, job_id=job_id)
                    return combined
                await update_index_job(
                    db, job_id, status="done",
                    finished_at=datetime.now(timezone.utc),
                    stats=json.dumps(combined),
                    error=None,
                )
        finally:
            await task_engine.dispose()

    return combined


def _normalize(s: str) -> str:
    """Normalize a string for fuzzy comparison: lowercase, replace [-_/] with spaces."""
    return re.sub(r'[-_/]+', ' ', s.lower()).strip()


def _build_module_lookup(modules: list[ParsedModule]) -> list[tuple[str, str]]:
    """Build a list of (normalized_key, module_path) for fuzzy tag matching.

    Each module gets multiple keys:
      - full path normalized: 'waf/waf_global_block_aml_acl' → 'waf waf global block aml acl'
      - module name normalized: 'waf_global_block_aml_acl' → 'waf global block aml acl'
    """
    entries: list[tuple[str, str]] = []
    for m in modules:
        entries.append((_normalize(m.module_path), m.module_path))
        entries.append((_normalize(m.module_name), m.module_path))
    return entries


def _match_tag_to_modules(
    tag: str,
    module_entries: list[tuple[str, str]],
) -> list[str] | None:
    """Match a git tag to module(s) using normalized fuzzy matching.

    Examples with real data:
      'acm-1.0.0'                    → prefix 'acm'     → matches acm/project, acm/subdomain
      'waf-global-block-aml-1.0.0'   → prefix 'waf global block aml'
                                        → matches 'waf waf global block aml acl' (contains)
      'cloudfront-itis-iwa-1.0.0'    → prefix 'cloudfront itis iwa'
                                        → matches 'cloudfront itis iwa'
      'redis-1.0.0'                  → prefix 'redis'   → matches 'elasticache redis'
      'v1.40', '1.16'               → repo-wide, return None
      'vpc_peering-1.0.0' (when module 'vpc_peering' is absent from HEAD)
                                     → return [] (skip tag; do NOT index all modules)

    Return values:
      None     → tag is repo-wide (pure version or non-versioned ref) → index full repo
      []       → tag has a module-name prefix but matches no known module → SKIP tag
      [paths]  → tag belongs to these specific modules → index only them
    """
    # Pure version tags → index full repo
    if re.match(r'^v?\d+(\.\d+)*$', tag):
        return None

    # Extract prefix before version number
    m = re.match(r'^(.+?)-(\d+\.\d+.*)$', tag)
    if not m:
        return None  # no version pattern (e.g. 'OPS-4029-pre-merge') → full repo

    prefix = _normalize(m.group(1))
    prefix_tokens = prefix.split()

    # Score each module entry: how well does the prefix match?
    scored: list[tuple[float, str]] = []
    for norm_key, module_path in module_entries:
        key_tokens = norm_key.split()

        # Check if all prefix tokens appear in the key (in order)
        if _is_subsequence(prefix_tokens, key_tokens):
            # Score: ratio of matched tokens to total tokens (prefer tighter matches)
            score = len(prefix_tokens) / len(key_tokens)
            scored.append((score, module_path))

    if not scored:
        # Fallback 1: module name is a prefix of the tag prefix
        # e.g. module 'ecs' matches tag 'ecs_cluster-1.0.3'
        for norm_key, module_path in module_entries:
            key_tokens = norm_key.split()
            if _is_subsequence(key_tokens, prefix_tokens):
                score = len(key_tokens) / len(prefix_tokens) * 0.6
                scored.append((score, module_path))

    if not scored:
        # Fallback 2: check if prefix is contained as substring
        for norm_key, module_path in module_entries:
            if prefix.replace(' ', '') in norm_key.replace(' ', ''):
                scored.append((0.5, module_path))

    if not scored:
        # Prefix looks like a module-name tag (e.g. 'vpc_peering-1.0.0') but
        # no known module matches it. Return [] (NOT None) so the caller skips
        # this tag — otherwise we'd index ALL modules of the repo under an
        # unrelated module's tag (the original bug).
        log.warning("tag_prefix_no_match", tag=tag, prefix=prefix,
                    action="skip_tag")
        return []

    # Deduplicate paths and pick the best matches
    best_score = max(s for s, _ in scored)
    matched_paths = list({path for score, path in scored if score >= best_score * 0.8})

    return matched_paths


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Check if all tokens in needle appear in haystack in order."""
    it = iter(haystack)
    return all(token in it for token in needle)


# -- Git helpers --------------------------------------------------------------

def _clone_or_pull(repo_url: str, branch: str,
                   checkout_ref: str | None = None) -> Path:
    repo_name = _repo_name_from_url(repo_url)
    local_path = Path(settings.repo_cache_dir) / repo_name
    local_path.mkdir(parents=True, exist_ok=True)

    if (local_path / ".git").exists():
        log.info("pulling_repo", path=str(local_path), branch=branch)
        repo = git.Repo(local_path)
        repo.remotes.origin.fetch(tags=True, force=True)
        ref = checkout_ref or branch
        repo.git.checkout(ref)
        if not checkout_ref:
            repo.remotes.origin.pull()
    else:
        log.info("cloning_repo", url=repo_url, branch=branch)
        # Use git CLI directly — GitPython's clone_from has issues with
        # multi_options/no_single_branch in some versions
        g = git.cmd.Git()
        g.clone(repo_url, str(local_path), branch=branch, no_single_branch=True)
        if checkout_ref:
            repo = git.Repo(local_path)
            repo.git.checkout(checkout_ref)

    return local_path


def _discover_tags(repo_dir: Path) -> list[str]:
    """List git tags matching the configured pattern, sorted newest first."""
    repo = git.Repo(repo_dir)
    pattern = re.compile(settings.tag_pattern)
    tags = [t.name for t in repo.tags if pattern.match(t.name)]
    tags.sort(key=_semver_sort_key, reverse=True)
    return tags[:settings.max_tags_to_index]


def _semver_sort_key(tag: str) -> tuple[int, int, int]:
    """Extract (major, minor, patch) for sorting; non-semver sorts to (0,0,0)."""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", tag)
    if not m:
        return (0, 0, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def clear_repo_cache(repo_url: str) -> None:
    """Delete cached clone for a repo so next index does a fresh clone."""
    repo_name = _repo_name_from_url(repo_url)
    local_path = Path(settings.repo_cache_dir) / repo_name
    if local_path.exists():
        log.info("clearing_repo_cache", path=str(local_path))
        shutil.rmtree(local_path)


# -- Module description -------------------------------------------------------

async def _upsert_with_raw_embedding(
    db, module: ParsedModule, embedding_str: str, description: str,
    commit_sha: str | None, job_id: str | None, code_hash: str,
):
    """Upsert using a pre-computed embedding string (from code_hash reuse)."""
    from sqlalchemy import text as sa_text
    await db.execute(
        sa_text("""
            INSERT INTO modules
                (repo, module_name, module_path, version, tags, variables, outputs,
                 resources, description, raw_code, embedding, commit_sha, job_id, code_hash)
            VALUES
                (:repo, :module_name, :module_path, :version, :tags, CAST(:variables AS jsonb),
                 CAST(:outputs AS jsonb), :resources, :description, :raw_code,
                 CAST(:embedding AS vector), :commit_sha,
                 (SELECT id FROM index_jobs WHERE id = CAST(:job_id AS uuid)),
                 :code_hash)
            ON CONFLICT (repo, module_path, version)
            DO UPDATE SET
                tags = EXCLUDED.tags, variables = EXCLUDED.variables,
                outputs = EXCLUDED.outputs, resources = EXCLUDED.resources,
                description = EXCLUDED.description, raw_code = EXCLUDED.raw_code,
                embedding = EXCLUDED.embedding, commit_sha = EXCLUDED.commit_sha,
                job_id = EXCLUDED.job_id, code_hash = EXCLUDED.code_hash,
                indexed_at = now()
        """),
        {
            "repo": module.repo, "module_name": module.module_name,
            "module_path": module.module_path, "version": module.version,
            "tags": module.tags,
            "variables": __import__("json").dumps(module.variables),
            "outputs": __import__("json").dumps(module.outputs),
            "resources": module.resources, "description": description,
            "raw_code": module.raw_code[:50_000], "embedding": embedding_str,
            "commit_sha": commit_sha, "job_id": job_id, "code_hash": code_hash,
        },
    )
    await db.commit()


_DESCRIPTION_SYSTEM_PROMPT = load_prompt("indexer/description.md")


def _build_description_prompt(module: ParsedModule) -> str:
    var_details = []
    for name, cfg in list(module.variables.items())[:15]:
        parts = [name]
        if isinstance(cfg, dict):
            if cfg.get("type"):
                parts.append(f"({cfg['type']})")
            if cfg.get("description"):
                parts.append(f"— {cfg['description']}")
            if cfg.get("default") is not None:
                parts.append(f"[default: {cfg['default']}]")
        var_details.append(" ".join(parts))

    out_details = []
    for name, cfg in list(module.outputs.items())[:10]:
        desc = cfg.get("description", "") if isinstance(cfg, dict) else ""
        out_details.append(f"{name}" + (f" — {desc}" if desc else ""))

    # Extract module calls from dependencies to make them explicit
    module_calls = [d for d in module.dependencies if d] if module.dependencies else []
    calls_str = ", ".join(module_calls) if module_calls else "none"

    resources = set(module.resources)
    resources_str = ", ".join(resources) or "none"

    header = (
        f"Module: {module.module_name}\n"
        f"Repository: {module.repo}\n"
        f"Path: {module.module_path}\n"
        f"Tags: {', '.join(module.tags) or 'none'}\n"
        f"Resources created (ONLY these): {resources_str}\n"
        f"Child module calls (NOT resources — these are other modules it invokes): {calls_str}\n"
    )

    return (
        header + "\n"
        f"Variables:\n" + "\n".join(f"  - {v}" for v in var_details) + "\n\n"
        f"Outputs:\n" + "\n".join(f"  - {o}" for o in out_details) + "\n\n"
        f"Code:\n{module.raw_code[:3000]}"
    )


def _description_fallback(module: ParsedModule) -> str:
    return (
        f"Terraform module '{module.module_name}' from '{module.repo}'. "
        f"Resources: {', '.join(set(module.resources)) or 'none'}. "
        f"Tags: {', '.join(module.tags) or 'none'}."
    )


_DESC_EVAL_SYSTEM = load_prompt("indexer/description_eval.md")


def _eval_prompt(module: ParsedModule, description: str) -> str:
    """Build evaluation prompt with full metadata context."""
    resources_str = ", ".join(set(module.resources)) or "none"
    vars_str = ", ".join(module.variables.keys()) or "none"
    deps = [d for d in module.dependencies if d] if module.dependencies else []
    calls_str = ", ".join(deps) if deps else "none"
    return (
        f"Module: {module.module_name} (repo: {module.repo})\n"
        f"Resources (ONLY these are created by this module): {resources_str}\n"
        f"Child module calls (other modules it invokes, NOT its own resources): {calls_str}\n"
        f"Variables: {vars_str}\n\n"
        f"Generated description:\n{description}"
    )


def _evaluate_description(module: ParsedModule, description: str) -> tuple[int, str]:
    """Evaluate a generated description against the module metadata.

    Returns (score 1-5, issues). On LLM failure returns (3, "eval_failed").
    """
    import json as _json

    prompt = _eval_prompt(module, description)

    raw = llm.describe(prompt, system=_DESC_EVAL_SYSTEM, max_tokens=150)
    if not raw:
        return 3, "eval_failed"

    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = _json.loads(cleaned)
        score = int(data.get("score", 3))
        issues = str(data.get("issues", ""))
        return max(1, min(5, score)), issues
    except (ValueError, _json.JSONDecodeError):
        return 3, "eval_parse_failed"


async def _aevaluate_description(module: ParsedModule, description: str) -> tuple[int, str]:
    """Async version of _evaluate_description."""
    import json as _json

    prompt = _eval_prompt(module, description)

    raw = await llm.adescribe(prompt, system=_DESC_EVAL_SYSTEM, max_tokens=150)
    if not raw:
        return 3, "eval_failed"

    try:
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = _json.loads(cleaned)
        score = int(data.get("score", 3))
        issues = str(data.get("issues", ""))
        return max(1, min(5, score)), issues
    except (ValueError, _json.JSONDecodeError):
        return 3, "eval_parse_failed"


def _generate_description(module: ParsedModule) -> str:
    prompt = _build_description_prompt(module)
    result = llm.describe(prompt, system=_DESCRIPTION_SYSTEM_PROMPT)
    if not result:
        return _description_fallback(module)

    score, issues = _evaluate_description(module, result)
    log.info("description_eval", module=module.module_name, score=score, issues=issues)

    if score >= 3:
        return result

    # Retry once with feedback
    retry_prompt = (
        f"{prompt}\n\n"
        f"Your previous description had issues: {issues}\n"
        f"Write a corrected description."
    )
    retry_result = llm.describe(retry_prompt, system=_DESCRIPTION_SYSTEM_PROMPT)
    if retry_result:
        retry_score, _ = _evaluate_description(module, retry_result)
        log.info("description_eval_retry", module=module.module_name, score=retry_score)
        if retry_score >= 3:
            return retry_result

    # Both attempts scored low — use fallback
    log.warning("description_quality_low", module=module.module_name,
                score=score, issues=issues)
    return _description_fallback(module)


async def _agenerate_description(module: ParsedModule) -> str:
    prompt = _build_description_prompt(module)
    result = await llm.adescribe(prompt, system=_DESCRIPTION_SYSTEM_PROMPT)
    if not result:
        return _description_fallback(module)

    score, issues = await _aevaluate_description(module, result)
    log.info("description_eval", module=module.module_name, score=score, issues=issues)

    if score >= 3:
        return result

    # Retry once with feedback
    retry_prompt = (
        f"{prompt}\n\n"
        f"Your previous description had issues: {issues}\n"
        f"Write a corrected description."
    )
    retry_result = await llm.adescribe(retry_prompt, system=_DESCRIPTION_SYSTEM_PROMPT)
    if retry_result:
        retry_score, _ = await _aevaluate_description(module, retry_result)
        log.info("description_eval_retry", module=module.module_name, score=retry_score)
        if retry_score >= 3:
            return retry_result

    # Both attempts scored low — use fallback
    log.warning("description_quality_low", module=module.module_name,
                score=score, issues=issues)
    return _description_fallback(module)


def _repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    return name.removesuffix(".git")
