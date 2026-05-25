from uuid import UUID
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.vector_store import get_db, create_index_job, delete_index_job, update_index_job
from app.core import graph as graph_db
from app.core.auth import AuthenticatedUser, require_admin, require_user
from app.models.schemas import IndexJobCreate, IndexJobResponse
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
    """Re-run indexing based on a previous job's parameters. Deletes repo cache first."""
    old_job = await _get_job(db, job_id)
    if not old_job:
        raise HTTPException(status_code=404, detail="Job not found")
    if not old_job.get("repo_url"):
        raise HTTPException(status_code=400, detail="Original job has no repo_url — cannot reindex")

    new_job_id = await create_index_job(
        db,
        repo=old_job["repo"],
        branch=old_job.get("branch") or "main",
        commit_sha=None,
        triggered_by="reindex",
        repo_url=old_job["repo_url"],
    )

    reindex_branch = old_job.get("branch") or "main"
    index_repository_task.delay(
        repo_url=old_job["repo_url"],
        branch=reindex_branch,
        commit_sha=None,
        job_id=str(new_job_id),
        version=reindex_branch,
        discover_tags=True,
        force_clone=True,
        force=True,
    )

    job = await _get_job(db, new_job_id)
    return IndexJobResponse(**job)


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


@router.get("/", response_model=list[IndexJobResponse])
async def list_jobs(
    repo: str | None = None,
    limit: int = 20,
    user: AuthenticatedUser = require_user,
    db: AsyncSession = Depends(get_db),
):
    """List recent indexing jobs."""
    condition = "WHERE repo = :repo" if repo else ""
    result = await db.execute(
        text(f"SELECT * FROM index_jobs {condition} ORDER BY started_at DESC NULLS LAST LIMIT :limit"),
        {"repo": repo, "limit": limit},
    )
    return [IndexJobResponse(**dict(r)) for r in result.mappings().all()]


async def _get_job(db: AsyncSession, job_id: UUID) -> dict | None:
    result = await db.execute(
        text("SELECT * FROM index_jobs WHERE id = :id"),
        {"id": job_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None
