"""
Consumer repo indexing API routes.

POST /consumer/            — trigger consumer repo indexing
GET  /consumer/            — list consumer index jobs
GET  /consumer/{job_id}    — get job status
POST /consumer/{job_id}/reindex — re-run consumer indexing
POST /consumer/distill     — manually trigger convention distillation
"""

from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.vector_store import get_db, create_consumer_index_job
from app.core.auth import AuthenticatedUser, require_admin, require_user
from app.models.schemas import ConsumerIndexJobCreate, ConsumerIndexJobResponse, PaginatedConsumerJobs
from app.workers.celery_app import index_consumer_repository_task, distill_conventions_task

router = APIRouter(prefix="/consumer", tags=["consumer-indexing"])


@router.post("/", response_model=ConsumerIndexJobResponse, status_code=202)
async def trigger_consumer_indexing(
    payload: ConsumerIndexJobCreate,
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    """Trigger indexing of a consumer repository (usage extraction)."""
    repo_name = payload.repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    job_id = await create_consumer_index_job(
        db,
        repo=repo_name,
        branch=payload.branch,
        commit_sha=payload.commit_sha,
        triggered_by=payload.triggered_by,
        repo_url=payload.repo_url,
    )

    index_consumer_repository_task.apply_async(
        kwargs=dict(
            repo_url=payload.repo_url,
            branch=payload.branch,
            commit_sha=payload.commit_sha,
            job_id=str(job_id),
            force_clone=payload.force_clone,
            run_distillation=payload.run_distillation,
        ),
        task_id=str(job_id),
    )

    job = await _get_job(db, job_id)
    return ConsumerIndexJobResponse(**job)


@router.get("/{job_id}", response_model=ConsumerIndexJobResponse)
async def get_consumer_job_status(job_id: UUID, user: AuthenticatedUser = require_user, db: AsyncSession = Depends(get_db)):
    """Check status of a consumer indexing job."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return ConsumerIndexJobResponse(**job)


@router.get("/", response_model=PaginatedConsumerJobs)
async def list_consumer_jobs(
    repo: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: AuthenticatedUser = require_user,
    db: AsyncSession = Depends(get_db),
):
    """List recent consumer indexing jobs (paginated)."""
    condition = "WHERE repo = :repo" if repo else ""
    params: dict = {"repo": repo, "limit": limit, "offset": offset}
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM consumer_index_jobs {condition}"), params,
    )
    total = count_result.scalar()
    result = await db.execute(
        text(f"""SELECT * FROM consumer_index_jobs {condition}
            ORDER BY CASE status
                WHEN 'running' THEN 0 WHEN 'pending' THEN 1
                WHEN 'failed' THEN 2 WHEN 'done' THEN 3
                ELSE 4 END,
            started_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset"""),
        params,
    )
    items = [ConsumerIndexJobResponse(**dict(r)) for r in result.mappings().all()]
    return PaginatedConsumerJobs(total=total, limit=limit, offset=offset, items=items)


@router.post("/{job_id}/cancel")
async def cancel_consumer_job(job_id: UUID, user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Cancel a running/pending consumer indexing job. Revokes the Celery task."""
    from app.core.vector_store import update_consumer_index_job
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is {job['status']}, cannot cancel")
    from datetime import datetime, timezone
    from app.workers.celery_app import celery_app
    celery_app.control.revoke(str(job_id), terminate=True, signal="SIGTERM")
    await update_consumer_index_job(db, job_id, status="cancelled",
                                    finished_at=datetime.now(timezone.utc),
                                    error="Cancelled by user")
    return {"status": "cancelled", "job_id": str(job_id)}


@router.delete("/{job_id}")
async def delete_consumer_job(job_id: UUID, user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Delete a consumer job and all knowledge_snippets it produced."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    repo_name = job["repo"]

    # Delete all usage snippets from this consumer repo
    usage_result = await db.execute(
        text("DELETE FROM knowledge_snippets WHERE consumer_repo = :repo AND kind = 'usage'"),
        {"repo": repo_name},
    )
    usages_deleted = usage_result.rowcount

    # Clean up orphaned conventions — modules that no longer have ANY usage
    conv_result = await db.execute(
        text("""
            DELETE FROM knowledge_snippets
            WHERE kind LIKE 'convention.%%'
              AND NOT EXISTS (
                  SELECT 1 FROM knowledge_snippets u
                  WHERE u.module_ref = knowledge_snippets.module_ref
                    AND u.kind = 'usage'
              )
        """),
    )
    conventions_deleted = conv_result.rowcount

    # Delete the job itself
    await db.execute(
        text("DELETE FROM consumer_index_jobs WHERE id = :id"),
        {"id": job_id},
    )
    await db.commit()

    return {
        "status": "deleted",
        "job_id": str(job_id),
        "usages_deleted": usages_deleted,
        "conventions_deleted": conventions_deleted,
    }


@router.post("/{job_id}/reindex", response_model=ConsumerIndexJobResponse, status_code=202)
async def reindex_consumer(job_id: UUID, user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Re-run consumer indexing, reusing the same job record."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("repo_url"):
        raise HTTPException(status_code=400, detail="Job has no repo_url")
    if job["status"] in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}")

    # Reset existing job to pending state
    await db.execute(
        text("""
            UPDATE consumer_index_jobs
            SET status = 'pending', started_at = NOW(), finished_at = NULL,
                commit_sha = NULL, stats = NULL, error = NULL,
                triggered_by = 'reindex'
            WHERE id = :job_id
        """),
        {"job_id": job_id},
    )
    await db.commit()

    index_consumer_repository_task.apply_async(
        kwargs=dict(
            repo_url=job["repo_url"],
            branch=job.get("branch") or "main",
            commit_sha=None,
            job_id=str(job_id),
            force_clone=True,
            run_distillation=True,
        ),
        task_id=str(job_id),
    )

    updated_job = await _get_job(db, job_id)
    return ConsumerIndexJobResponse(**updated_job)


@router.post("/distill")
async def trigger_distillation(
    module_refs: list[str] | None = None,
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger convention distillation.

    If module_refs is empty/None, distills all modules that have usage snippets.
    """
    if not module_refs:
        result = await db.execute(
            text("SELECT DISTINCT module_ref FROM knowledge_snippets WHERE kind = 'usage'")
        )
        module_refs = [row["module_ref"] for row in result.mappings().all()]

    if not module_refs:
        return {"status": "no_modules", "message": "No modules with usage data found"}

    distill_conventions_task.delay(module_refs=module_refs)

    return {
        "status": "queued",
        "modules": len(module_refs),
        "module_refs": module_refs,
    }


@router.post("/distill-weak")
async def trigger_weak_distillation(
    max_score: int = 3,
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    """Re-distill only weak conventions (low eval_score or stale).

    Finds modules that have at least one convention with eval_score <= max_score
    or stale = TRUE, and triggers distillation only for those modules.
    """
    result = await db.execute(
        text("""
            SELECT DISTINCT module_ref FROM knowledge_snippets
            WHERE kind LIKE 'convention.%%'
              AND (eval_score <= :max_score OR stale = TRUE)
              AND EXISTS (
                  SELECT 1 FROM knowledge_snippets u
                  WHERE u.module_ref = knowledge_snippets.module_ref
                    AND u.kind = 'usage'
              )
        """),
        {"max_score": max_score},
    )
    module_refs = [row["module_ref"] for row in result.mappings().all()]

    if not module_refs:
        return {
            "status": "no_modules",
            "message": f"No modules with conventions scoring <= {max_score} or stale",
        }

    # Count weak/stale conventions for reporting
    count_result = await db.execute(
        text("""
            SELECT
                COUNT(*) FILTER (WHERE eval_score <= :max_score AND NOT stale) AS weak,
                COUNT(*) FILTER (WHERE stale = TRUE) AS stale
            FROM knowledge_snippets
            WHERE kind LIKE 'convention.%%'
              AND module_ref = ANY(:refs)
        """),
        {"max_score": max_score, "refs": module_refs},
    )
    counts = count_result.mappings().first()

    distill_conventions_task.delay(module_refs=module_refs)

    return {
        "status": "queued",
        "modules": len(module_refs),
        "weak_conventions": counts["weak"],
        "stale_conventions": counts["stale"],
        "max_score_filter": max_score,
        "module_refs": module_refs,
    }


async def _get_job(db: AsyncSession, job_id: UUID) -> dict | None:
    result = await db.execute(
        text("SELECT * FROM consumer_index_jobs WHERE id = :id"),
        {"id": job_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None
