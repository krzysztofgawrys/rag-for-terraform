import asyncio
import time as _time
from celery import Celery
from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "terraform_rag",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_expires=3600,
    worker_prefetch_multiplier=1,      # one task at a time per worker
    task_acks_late=False,              # ack immediately — prevents redelivery of long tasks
    task_reject_on_worker_lost=True,   # reject (don't requeue) if worker dies
    broker_transport_options={
        "visibility_timeout": 43200,   # 12h — task won't be redelivered
    },
)


@celery_app.task(name="index_repository", bind=True, max_retries=2)
def index_repository_task(self, repo_url: str, branch: str,
                           commit_sha: str | None, job_id: str,
                           version: str = "main",
                           discover_tags: bool = False,
                           force_clone: bool = False,
                           force: bool = False):
    """Celery task wrapping async indexer."""
    from app.services.indexer import run_indexing, run_tag_indexing, clear_repo_cache
    start = _time.perf_counter()
    error_text = None
    result = None
    # Mark running immediately so UI reflects the real state
    asyncio.run(_mark_index_job_running(job_id))
    try:
        if force_clone:
            clear_repo_cache(repo_url)
        if discover_tags:
            result = asyncio.run(run_tag_indexing(repo_url, branch, job_id, force=force))
        else:
            result = asyncio.run(run_indexing(repo_url, branch, commit_sha, job_id,
                                              version=version, force=force))
        return result
    except Exception as exc:
        error_text = str(exc)
        raise self.retry(exc=exc, countdown=30)
    finally:
        from app.core.audit import emit_sync
        emit_sync(
            "worker", "task:index_repository",
            status="error" if error_text else "success",
            duration_ms=int((_time.perf_counter() - start) * 1000),
            request_data={"repo_url": repo_url, "branch": branch, "job_id": job_id,
                          "version": version, "discover_tags": discover_tags},
            response_data=result,
            error=error_text,
            metadata={"task_id": self.request.id, "retries": self.request.retries},
        )


@celery_app.task(name="index_consumer_repository", bind=True, max_retries=2)
def index_consumer_repository_task(
    self,
    repo_url: str,
    branch: str,
    commit_sha: str | None,
    job_id: str,
    force_clone: bool = False,
    run_distillation: bool = True,
):
    """Celery task: index a consumer repo → usage snippets → distillation."""
    from app.services.consumer_indexer import run_consumer_indexing
    from app.services.convention_distiller import run_distillation as distill
    import structlog
    _log = structlog.get_logger()
    start = _time.perf_counter()
    error_text = None
    result = None
    asyncio.run(_mark_consumer_job_running(job_id))
    try:
        # Phase 1: consumer indexing (parse → embed → store)
        # Retry the whole task only if THIS phase fails.
        stats = asyncio.run(run_consumer_indexing(
            repo_url, branch, commit_sha, job_id, force_clone,
        ))

        # Phase 2: distillation — runs in its own try/except.
        # A distillation failure should NOT cause re-indexing of the consumer repo.
        if run_distillation and stats.get("affected_modules"):
            base_stats = {k: v for k, v in stats.items() if k != "affected_modules"}
            try:
                distill_stats = asyncio.run(distill(
                    stats["affected_modules"],
                    job_id=job_id,
                    base_stats=base_stats,
                ))
                stats["distillation"] = distill_stats
            except Exception as distill_exc:
                _log.error("distillation_failed_non_fatal",
                           error=str(distill_exc),
                           affected_modules=stats["affected_modules"])
                # Preserve partial stats that were written to DB during
                # distillation — don't overwrite with just an error string.
                stats.setdefault("distillation", {})
                stats["distillation"]["error"] = str(distill_exc)[:500]

        # Mark job as done — merge with partial stats already in DB
        # (distillation writes progress incrementally via update_consumer_index_job)
        if job_id:
            db_stats = {k: v for k, v in stats.items() if k != "affected_modules"}
            asyncio.run(_finalize_consumer_job(job_id, db_stats, merge=True))

        result = stats
        return result
    except Exception as exc:
        error_text = str(exc)
        if job_id:
            asyncio.run(_fail_consumer_job(job_id, str(exc)))
        raise self.retry(exc=exc, countdown=30)
    finally:
        from app.core.audit import emit_sync
        emit_sync(
            "worker", "task:index_consumer_repository",
            status="error" if error_text else "success",
            duration_ms=int((_time.perf_counter() - start) * 1000),
            request_data={"repo_url": repo_url, "branch": branch, "job_id": job_id,
                          "run_distillation": run_distillation},
            response_data=result,
            error=error_text,
            metadata={"task_id": self.request.id, "retries": self.request.retries},
        )


async def _finalize_consumer_job(job_id: str, stats: dict, merge: bool = False):
    """Mark consumer job as done with final stats.

    If merge=True, reads existing stats from DB and deep-merges instead of
    overwriting.  This preserves partial distillation progress written
    incrementally during run_distillation().
    """
    import json
    from datetime import datetime, timezone
    from sqlalchemy import text
    from app.core.vector_store import make_session_factory, update_consumer_index_job
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            final_stats = stats
            if merge:
                row = await db.execute(
                    text("SELECT stats FROM consumer_index_jobs WHERE id = :id"),
                    {"id": job_id},
                )
                existing_raw = row.scalar_one_or_none()
                if existing_raw:
                    existing = json.loads(existing_raw) if isinstance(existing_raw, str) else existing_raw
                    # Deep merge: stats keys overwrite, but preserve
                    # distillation sub-dict from DB if our stats only has error
                    for k, v in stats.items():
                        if k == "distillation" and isinstance(v, dict) and isinstance(existing.get(k), dict):
                            existing[k].update(v)
                        else:
                            existing[k] = v
                    final_stats = existing

            # Don't overwrite 'cancelled' status
            current_status = (await db.execute(
                text("SELECT status FROM consumer_index_jobs WHERE id = :id"),
                {"id": job_id},
            )).scalar_one_or_none()
            if current_status != "cancelled":
                await update_consumer_index_job(
                    db, job_id,
                    status="done",
                    finished_at=datetime.now(timezone.utc),
                    stats=json.dumps(final_stats),
                    error=None,
                )
    finally:
        await engine.dispose()


async def _fail_consumer_job(job_id: str, error: str):
    """Mark consumer job as failed."""
    from datetime import datetime, timezone
    from app.core.vector_store import make_session_factory, update_consumer_index_job
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            await update_consumer_index_job(
                db, job_id,
                status="failed",
                finished_at=datetime.now(timezone.utc),
                error=error[:2000],
            )
    finally:
        await engine.dispose()


async def _mark_consumer_job_running(job_id: str):
    """Mark consumer job as running immediately when Celery picks it up."""
    from datetime import datetime, timezone
    from app.core.vector_store import make_session_factory, update_consumer_index_job
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            await update_consumer_index_job(db, job_id, status="running",
                                            started_at=datetime.now(timezone.utc))
    finally:
        await engine.dispose()


async def _mark_index_job_running(job_id: str):
    """Mark index job as running immediately when Celery picks it up."""
    from datetime import datetime, timezone
    from app.core.vector_store import make_session_factory, update_index_job
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            await update_index_job(db, job_id, status="running",
                                   started_at=datetime.now(timezone.utc))
    finally:
        await engine.dispose()


@celery_app.task(name="distill_conventions", bind=True, max_retries=1)
def distill_conventions_task(self, module_refs: list[str]):
    """Celery task: run convention distillation for specific module_refs."""
    from app.services.convention_distiller import run_distillation as distill
    start = _time.perf_counter()
    error_text = None
    result = None
    try:
        result = asyncio.run(distill(module_refs))
        return result
    except Exception as exc:
        error_text = str(exc)
        raise self.retry(exc=exc, countdown=30)
    finally:
        from app.core.audit import emit_sync
        emit_sync(
            "worker", "task:distill_conventions",
            status="error" if error_text else "success",
            duration_ms=int((_time.perf_counter() - start) * 1000),
            request_data={"module_refs": module_refs},
            response_data=result,
            error=error_text,
            metadata={"task_id": self.request.id, "retries": self.request.retries},
        )
