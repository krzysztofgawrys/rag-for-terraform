"""
Consumer Indexer — indexes Terraform consumer repos into knowledge_snippets.

Pipeline:
  1. Clone/pull consumer repo
  2. Parse all module {} blocks → ParsedUsage list
  3. Resolve module_refs against indexed modules (skip unknown)
  4. Build usage summaries (template, no LLM)
  5. Embed summaries
  6. Delete old snippets for this consumer repo (idempotent)
  7. Insert new usage snippets
  8. Enqueue convention distillation for affected module_refs
"""

import json
import shutil
import structlog
from datetime import datetime, timezone
from pathlib import Path

import git

from app.core.config import get_settings
from app.core.embeddings import embed_query
from app.core.consumer_parser import (
    parse_consumer_repo, build_usage_summary, build_compose_summary,
)
from app.core import vector_store as vs
from app.core.embeddings import embed_query as _embed_query

log = structlog.get_logger()
settings = get_settings()


async def run_consumer_indexing(
    repo_url: str,
    branch: str = "main",
    commit_sha: str | None = None,
    job_id: str | None = None,
    force_clone: bool = False,
) -> dict:
    """Main consumer indexing pipeline. Returns stats dict."""
    repo_name = _repo_name_from_url(repo_url)

    engine, SessionLocal = vs.make_session_factory()
    try:
        async with SessionLocal() as db:
            # Update job status (clear error/finished_at from previous retry)
            if job_id:
                await vs.update_consumer_index_job(
                    db, job_id,
                    status="running",
                    started_at=datetime.now(timezone.utc),
                    finished_at=None,
                    error=None,
                )

            try:
                stats = await _index_consumer(
                    db, repo_url, branch, commit_sha, repo_name, force_clone,
                    job_id=job_id,
                )

                if job_id:
                    await vs.update_consumer_index_job(
                        db, job_id,
                        stats=json.dumps(stats),
                    )

                return stats

            except Exception as e:
                log.error("consumer_indexing_failed", repo=repo_name, error=str(e))
                raise
    finally:
        await engine.dispose()


async def _index_consumer(
    db,
    repo_url: str,
    branch: str,
    commit_sha: str | None,
    repo_name: str,
    force_clone: bool,
    job_id: str | None = None,
) -> dict:
    """Core indexing logic."""
    # 1. Clone/pull
    if force_clone:
        clear_consumer_cache(repo_url)
    local_path = _clone_or_pull(repo_url, branch, commit_sha)

    # Get actual commit SHA
    try:
        repo = git.Repo(local_path)
        actual_sha = repo.head.commit.hexsha
    except Exception:
        actual_sha = commit_sha or ""

    # 2. Parse all module {} blocks
    usages = parse_consumer_repo(str(local_path), repo_name, actual_sha)
    log.info("consumer_parsed", repo=repo_name, total_usages=len(usages))

    if not usages:
        return {"parsed": 0, "resolved": 0, "embedded": 0, "affected_modules": []}

    # 3. Resolve module_refs — check which ones exist in our modules table
    known_refs = await _resolve_known_modules(db, usages)
    resolved_usages = [u for u in usages if u.module_ref in known_refs]
    log.info("consumer_resolved", repo=repo_name,
             total=len(usages), resolved=len(resolved_usages),
             unknown=len(usages) - len(resolved_usages))

    if not resolved_usages:
        return {"parsed": len(usages), "resolved": 0, "embedded": 0, "affected_modules": []}

    # 4. Get module_refs affected BEFORE deleting old snippets
    old_affected = await vs.get_affected_module_refs(db, repo_name)

    # 5. Delete old usage snippets for this consumer repo
    deleted = await vs.delete_snippets_by_consumer(db, repo_name)
    log.info("consumer_old_deleted", repo=repo_name, deleted=deleted)

    # 6. Build summaries, embed, and insert
    embedded_count = 0
    for usage in resolved_usages:
        summary = build_usage_summary(usage)

        # Embed the summary
        embedding = embed_query(summary, query_type="search")

        # Determine scope
        scope_parts = []
        if usage.env:
            scope_parts.append(f"env:{usage.env}")
        if usage.region:
            scope_parts.append(f"region:{usage.region}")
        scope = ", ".join(scope_parts) if scope_parts else None

        await vs.upsert_snippet(
            db,
            kind="usage",
            module_ref=usage.module_ref,
            summary=summary,
            embedding=embedding,
            evidence_count=1,
            scope=scope,
            source_locator=usage.source_locator,
            related_refs=usage.siblings if usage.siblings else None,
            consumer_repo=repo_name,
        )
        embedded_count += 1

        # Update job stats incrementally every 5 embeddings + check cancellation
        if job_id and embedded_count % 5 == 0:
            await vs.update_consumer_index_job(
                db, job_id,
                stats=json.dumps({
                    "parsed": len(usages),
                    "resolved": len(resolved_usages),
                    "embedded": embedded_count,
                }),
            )
            from sqlalchemy import text as _text
            _status = (await db.execute(
                _text("SELECT status FROM consumer_index_jobs WHERE id = :id"),
                {"id": job_id},
            )).scalar_one_or_none()
            if _status == "cancelled":
                log.info("consumer_indexing_cancelled", repo=repo_name,
                         embedded=embedded_count)
                return {
                    "parsed": len(usages),
                    "resolved": len(resolved_usages),
                    "embedded": embedded_count,
                    "affected_modules": list(set(
                        u.module_ref for u in resolved_usages[:embedded_count]
                    )),
                }

    # 6b. Emit compose-pattern snippets for files that wire 2+ modules
    # together. These are full-stack examples (e.g. a consumer repo's
    # modules/*/main.tf) that surface as references for "build me a
    # complete X" queries.
    compose_count = 0
    usages_by_file: dict[str, list] = {}
    for u in resolved_usages:
        usages_by_file.setdefault(u.consumer_path, []).append(u)
    for file_path, file_usages in usages_by_file.items():
        compose = build_compose_summary(file_usages)
        if not compose:
            continue
        compose_embedding = embed_query(compose, query_type="search")
        # Use first usage's source_locator stem (file-level, no line range)
        first_loc = file_usages[0].source_locator.split(":L")[0]
        # module_ref slot encodes the consumer file as the "pattern owner"
        compose_module_ref = f"compose:{repo_name}:{file_path}"
        await vs.upsert_snippet(
            db,
            kind="compose_pattern",
            module_ref=compose_module_ref,
            summary=compose,
            embedding=compose_embedding,
            evidence_count=len(file_usages),
            scope=None,
            source_locator=first_loc,
            related_refs=list({u.module_ref for u in file_usages}),
            consumer_repo=repo_name,
        )
        compose_count += 1
    if compose_count:
        log.info("consumer_compose_indexed", repo=repo_name, files=compose_count)

    # 6c. Recompute stack patterns across ALL consumer repos. This aggregates
    # compose_pattern rows by sorted module-set into kind='stack_pattern'
    # rows — surfacing canonical "VPC + ACM + microservice" combinations
    # used across multiple consumer repos.
    #
    # Protected by a Postgres advisory lock so concurrent workers don't
    # race. If our session's transaction was poisoned by an error inside
    # recompute_stack_patterns, rollback so subsequent UPDATEs (stats,
    # status) don't fail with "current transaction is aborted".
    try:
        await recompute_stack_patterns(db)
    except Exception as exc:
        log.warning("stack_pattern_recompute_failed", error=str(exc)[:200])
        try:
            await db.rollback()
        except Exception:
            pass

    # 7. Collect affected module_refs (old + new) for distillation
    new_affected = set(u.module_ref for u in resolved_usages)
    all_affected = list(new_affected | set(old_affected))

    log.info("consumer_indexed", repo=repo_name,
             embedded=embedded_count, compose=compose_count,
             affected_modules=len(all_affected))

    return {
        "parsed": len(usages),
        "resolved": len(resolved_usages),
        "embedded": embedded_count,
        "compose_files": compose_count,
        "affected_modules": all_affected,
    }


# Postgres advisory lock id used to serialise stack-pattern recomputes.
# Multiple consumer indexing tasks run in parallel; without this lock they
# race on the partial unique index `snippets_convention_upsert_idx`
# (which covers kind='stack_pattern').
_STACK_PATTERN_LOCK_ID = 834_291_8


async def recompute_stack_patterns(db) -> int:
    """Aggregate compose_pattern rows into stack_pattern rows.

    Groups every compose_pattern by its sorted module_ref set (the unique
    "stack signature"). Any signature observed in ≥2 distinct files becomes
    a stack_pattern row whose summary lists the modules and the number of
    consumer files using this combination.

    Acquires a Postgres advisory lock so concurrent consumer-index tasks
    don't race on the partial unique constraint. If another worker is
    already inside this function, we skip — the other worker will produce
    a fresh aggregate that already accounts for our compose rows.

    Returns the number of stack_pattern rows written (0 if we skipped).
    """
    from sqlalchemy import text
    import hashlib

    # Try to acquire the advisory lock without blocking. If another worker
    # holds it, just skip — they will recompute including our data.
    lock_acquired = (await db.execute(
        text("SELECT pg_try_advisory_lock(:lock_id)"),
        {"lock_id": _STACK_PATTERN_LOCK_ID},
    )).scalar()
    if not lock_acquired:
        log.info("stack_patterns_recompute_skipped",
                 reason="another_worker_running")
        return 0

    try:
        # Pull every compose_pattern with its related_refs
        rows = (await db.execute(
            text("""
                SELECT source_locator, related_refs, consumer_repo, evidence_count
                FROM knowledge_snippets
                WHERE kind = 'compose_pattern' AND related_refs IS NOT NULL
            """)
        )).mappings().all()

        # Group by sorted-refs signature
        groups: dict[tuple[str, ...], list[dict]] = {}
        for r in rows:
            refs = sorted(set(r["related_refs"] or []))
            if len(refs) < 2:
                continue
            key = tuple(refs)
            groups.setdefault(key, []).append(dict(r))

        # Clear previous aggregated stack_patterns before re-inserting
        await db.execute(
            text("DELETE FROM knowledge_snippets WHERE kind = 'stack_pattern' "
                 "AND module_ref LIKE 'stack:%%'"),
        )

        written = 0
        for refs_key, members in groups.items():
            if len(members) < 2:
                continue

            # Stack signature: short hash of the sorted module list
            sig = hashlib.md5(",".join(refs_key).encode()).hexdigest()[:12]
            sample_files = sorted({m["source_locator"] for m in members})[:5]
            consumer_count = len({m["consumer_repo"] for m in members})

            summary = (
                f"Stack pattern (signature {sig}): combination of "
                f"{len(refs_key)} modules used in {len(members)} files across "
                f"{consumer_count} consumer repo(s).\n\n"
                f"Modules in this stack:\n" +
                "\n".join(f"  - {r}" for r in refs_key) +
                f"\n\nExample files:\n" +
                "\n".join(f"  - {f}" for f in sample_files)
            )

            embedding = _embed_query(summary, query_type="search")

            await db.execute(
                text("""
                    INSERT INTO knowledge_snippets
                        (kind, module_ref, summary, embedding, evidence_count,
                         related_refs, updated_at)
                    VALUES
                        (:kind, :module_ref, :summary,
                         CAST(:embedding AS vector), :evidence_count,
                         :related_refs, now())
                    ON CONFLICT (module_ref, kind)
                        WHERE kind LIKE 'convention.%%' OR kind = 'stack_pattern'
                    DO UPDATE SET
                        summary = EXCLUDED.summary,
                        embedding = EXCLUDED.embedding,
                        evidence_count = EXCLUDED.evidence_count,
                        related_refs = EXCLUDED.related_refs,
                        updated_at = now()
            """),
                {
                    "kind": "stack_pattern",
                    "module_ref": f"stack:{sig}",
                    "summary": summary,
                    "embedding": str(embedding),
                    "evidence_count": len(members),
                    "related_refs": list(refs_key),
                },
            )
            written += 1

        await db.commit()
        log.info("stack_patterns_recomputed",
                 total_groups=len(groups),
                 written=written)
        return written
    finally:
        # Always release the advisory lock, even on exception.
        try:
            await db.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _STACK_PATTERN_LOCK_ID},
            )
        except Exception:
            pass  # lock released automatically on connection close


async def _resolve_known_modules(db, usages) -> set[str]:
    """Check which module_refs from usages exist in our modules table."""
    from sqlalchemy import text

    # Collect unique module_refs
    refs = set(u.module_ref for u in usages if u.module_ref)
    if not refs:
        return set()

    # Query modules table for matches
    # module_ref format: 'repo/path' — matches against modules.repo + modules.module_path
    known = set()
    for ref in refs:
        parts = ref.split("/", 1)
        if len(parts) == 2:
            repo, path = parts
        else:
            repo = parts[0]
            path = ""

        result = await db.execute(
            text("""
                SELECT 1 FROM modules
                WHERE repo = :repo AND module_path IN (:path, '.')
                LIMIT 1
            """),
            {"repo": repo, "path": path},
        )
        if result.first():
            known.add(ref)

    # Also try matching by repo name containing the ref (fallback path —
    # currently rarely hit because the primary loop above already matches
    # by (repo, path) split. Kept defensive in case module_ref comes in a
    # non-standard form.)
    for ref in refs - known:
        result = await db.execute(
            text("""
                SELECT DISTINCT repo || '/' || module_path AS full_ref
                FROM modules
                WHERE repo || '/' || module_path = :ref
                   OR module_path = :ref
                LIMIT 1
            """),
            {"ref": ref},
        )
        row = result.first()
        if row:
            # Use the matched row's full_ref — not the input ref — so that
            # downstream filtering (`u.module_ref in known_refs`) compares
            # against the canonical "repo/module_path" form stored in DB.
            known.add(row[0])
            if row[0] != ref:
                known.add(ref)  # also keep original for direct hits

    log.info("module_refs_resolved", total=len(refs), known=len(known),
             unknown=list(refs - known)[:10])
    return known


# -- Git helpers (reuse patterns from indexer.py) -----------------------------

def _clone_or_pull(repo_url: str, branch: str,
                   checkout_ref: str | None = None) -> Path:
    repo_name = _repo_name_from_url(repo_url)
    local_path = Path(settings.repo_cache_dir) / repo_name
    local_path.mkdir(parents=True, exist_ok=True)

    if (local_path / ".git").exists():
        log.info("pulling_consumer_repo", path=str(local_path), branch=branch)
        repo = git.Repo(local_path)
        repo.remotes.origin.fetch(tags=True, force=True)
        ref = checkout_ref or branch
        repo.git.checkout(ref)
        if not checkout_ref:
            repo.remotes.origin.pull()
    else:
        log.info("cloning_consumer_repo", url=repo_url, branch=branch)
        g = git.cmd.Git()
        g.clone(repo_url, str(local_path), branch=branch, no_single_branch=True)
        if checkout_ref:
            repo = git.Repo(local_path)
            repo.git.checkout(checkout_ref)

    return local_path


def clear_consumer_cache(repo_url: str) -> None:
    repo_name = _repo_name_from_url(repo_url)
    local_path = Path(settings.repo_cache_dir) / repo_name
    if local_path.exists():
        log.info("clearing_consumer_cache", path=str(local_path))
        shutil.rmtree(local_path)


def _repo_name_from_url(url: str) -> str:
    name = url.rstrip("/").split("/")[-1]
    return name.removesuffix(".git")
