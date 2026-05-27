from uuid import UUID
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.vector_store import get_db, create_index_job, delete_index_job, update_index_job
from app.core import graph as graph_db
from app.core.auth import AuthenticatedUser, require_admin, require_user
from app.models.schemas import IndexJobCreate, IndexJobResponse, PaginatedIndexJobs
from app.workers.celery_app import index_repository_task

router = APIRouter(prefix="/index", tags=["indexing"])


@router.post("/", response_model=IndexJobResponse, status_code=202)
async def trigger_indexing(
    payload: IndexJobCreate,
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    """Manually trigger repository indexing."""
    repo_name = payload.repo_url.rstrip("/").split("/")[-1].removesuffix(".git")

    job_id = await create_index_job(
        db,
        repo=repo_name,
        branch=payload.branch,
        commit_sha=payload.commit_sha,
        triggered_by=payload.triggered_by,
        repo_url=payload.repo_url,
    )

    index_repository_task.delay(
        repo_url=payload.repo_url,
        branch=payload.branch,
        commit_sha=payload.commit_sha,
        job_id=str(job_id),
        version=payload.branch,
        discover_tags=payload.discover_tags,
        force=payload.force,
    )

    job = await _get_job(db, job_id)
    return IndexJobResponse(**job)


@router.post("/{job_id}/reindex", response_model=IndexJobResponse, status_code=202)
async def reindex(job_id: UUID, user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Re-run indexing for an existing job (resets status, reuses the same job ID)."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not job.get("repo_url"):
        raise HTTPException(status_code=400, detail="Job has no repo_url - cannot reindex")
    if job["status"] in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is already {job['status']}")

    await update_index_job(db, job_id,
                           status="pending", started_at=None, finished_at=None,
                           error=None, stats=None)

    reindex_branch = job.get("branch") or "main"
    index_repository_task.delay(
        repo_url=job["repo_url"],
        branch=reindex_branch,
        commit_sha=None,
        job_id=str(job_id),
        version=reindex_branch,
        discover_tags=True,
        force_clone=True,
        force=True,
    )

    updated = await _get_job(db, job_id)
    return IndexJobResponse(**updated)


@router.post("/{job_id}/cancel")
async def cancel_job(job_id: UUID, user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Cancel a running/pending indexing job."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] not in ("pending", "running"):
        raise HTTPException(status_code=400, detail=f"Job is {job['status']}, cannot cancel")
    from datetime import datetime, timezone
    await update_index_job(db, job_id, status="cancelled",
                           finished_at=datetime.now(timezone.utc),
                           error="Cancelled by user")
    return {"status": "cancelled", "job_id": str(job_id)}


@router.delete("/{job_id}")
async def delete_job(job_id: UUID, user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Delete an index job and all modules it indexed."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    result = await delete_index_job(db, job_id)

    # Clean up dependency edges for deleted modules
    for m in result["modules"]:
        try:
            await graph_db.delete_module(m["repo"], m["module_path"], m["version"])
        except Exception:
            pass  # best-effort cleanup

    return {
        "status": "deleted",
        "job_id": str(job_id),
        "modules_deleted": result["modules_deleted"],
    }


@router.get("/{job_id}", response_model=IndexJobResponse)
async def get_job_status(job_id: UUID, user: AuthenticatedUser = require_user, db: AsyncSession = Depends(get_db)):
    """Check indexing task status."""
    job = await _get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return IndexJobResponse(**job)


@router.get("/", response_model=PaginatedIndexJobs)
async def list_jobs(
    repo: str | None = None,
    limit: int = 20,
    offset: int = 0,
    user: AuthenticatedUser = require_user,
    db: AsyncSession = Depends(get_db),
):
    """List recent indexing jobs (paginated)."""
    condition = "WHERE repo = :repo" if repo else ""
    params: dict = {"repo": repo, "limit": limit, "offset": offset}
    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM index_jobs {condition}"), params,
    )
    total = count_result.scalar()
    result = await db.execute(
        text(f"""SELECT * FROM index_jobs {condition}
            ORDER BY CASE status
                WHEN 'running' THEN 0 WHEN 'pending' THEN 1
                WHEN 'failed' THEN 2 WHEN 'done' THEN 3
                ELSE 4 END,
            started_at DESC NULLS LAST
            LIMIT :limit OFFSET :offset"""),
        params,
    )
    items = [IndexJobResponse(**dict(r)) for r in result.mappings().all()]
    return PaginatedIndexJobs(total=total, limit=limit, offset=offset, items=items)


async def _get_job(db: AsyncSession, job_id: UUID) -> dict | None:
    result = await db.execute(
        text("SELECT * FROM index_jobs WHERE id = :id"),
        {"id": job_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None
