"""
Webhook endpoints for GitHub and GitLab.
On push to main/master, automatically triggers repository re-indexing.
"""
import hmac
import hashlib
import json
import structlog
from fastapi import APIRouter, Request, HTTPException, Header
from typing import Optional

from app.core.config import get_settings
from app.core.vector_store import AsyncSessionLocal, create_index_job
from app.workers.celery_app import index_repository_task

router = APIRouter(prefix="/webhook", tags=["webhooks"])
log = structlog.get_logger()
settings = get_settings()

WATCHED_BRANCHES = {"main", "master"}


# -- GitHub --------------------------------------------------------------------

@router.post("/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(...),
    x_hub_signature_256: Optional[str] = Header(None),
):
    body = await request.body()

    # HMAC verification (always required)
    if not settings.github_webhook_secret:
        raise HTTPException(status_code=503, detail="Webhook secret not configured")
    _verify_github_signature(body, x_hub_signature_256)

    if x_github_event != "push":
        return {"status": "ignored", "reason": f"event={x_github_event}"}

    payload = json.loads(body)
    ref: str = payload.get("ref", "")
    commit_sha: str = payload.get("after", "")
    repo_data: dict = payload.get("repository", {})

    # Get SSH or HTTPS URL and validate against allowlist
    repo_url = repo_data.get("ssh_url") or repo_data.get("clone_url", "")
    repo_name = repo_data.get("full_name", repo_url)
    _validate_clone_url(repo_url)

    # Tag push → index specific version
    if ref.startswith("refs/tags/"):
        tag_name = ref.removeprefix("refs/tags/")
        default_branch = repo_data.get("default_branch", "main")
        job_id = await _enqueue_job(repo_url, default_branch, commit_sha,
                                     "github_webhook_tag")
        index_repository_task.delay(
            repo_url=repo_url, branch=default_branch,
            commit_sha=commit_sha, job_id=str(job_id),
            version=tag_name,
        )
        log.info("webhook_tag_accepted", repo=repo_name, tag=tag_name)
        return {"status": "accepted", "job_id": str(job_id), "tag": tag_name}

    # Branch push → index as "latest"
    branch = ref.removeprefix("refs/heads/")
    if branch not in WATCHED_BRANCHES:
        log.info("webhook_ignored", branch=branch, repo=repo_name)
        return {"status": "ignored", "reason": f"branch={branch}"}

    job_id = await _enqueue_job(repo_url, branch, commit_sha, "github_webhook")
    log.info("webhook_accepted", repo=repo_name, branch=branch, commit=commit_sha[:8])
    return {"status": "accepted", "job_id": str(job_id)}


# -- GitLab --------------------------------------------------------------------

@router.post("/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: Optional[str] = Header(None),
):
    # GitLab token verification (always required)
    if not settings.gitlab_webhook_token:
        raise HTTPException(status_code=503, detail="Webhook token not configured")
    if x_gitlab_token != settings.gitlab_webhook_token:
        raise HTTPException(status_code=401, detail="Invalid GitLab token")

    payload = await request.json()
    event = payload.get("object_kind", "")

    if event != "push":
        return {"status": "ignored", "reason": f"event={event}"}

    ref: str = payload.get("ref", "")
    branch = ref.removeprefix("refs/heads/")
    commit_sha: str = payload.get("after", "")
    project: dict = payload.get("project", {})
    repo_url = project.get("ssh_url_to_repo") or project.get("http_url_to_repo", "")
    _validate_clone_url(repo_url)

    if branch not in WATCHED_BRANCHES:
        return {"status": "ignored", "reason": f"branch={branch}"}

    job_id = await _enqueue_job(repo_url, branch, commit_sha, "gitlab_webhook")
    log.info("gitlab_webhook_accepted", repo=repo_url, branch=branch)
    return {"status": "accepted", "job_id": str(job_id)}


# -- Helpers -------------------------------------------------------------------

def _validate_clone_url(repo_url: str) -> None:
    """Reject clone URLs whose hostname is not in the allowlist."""
    import re
    allowed = {h.strip().lower() for h in settings.webhook_allowed_hosts.split(",") if h.strip()}
    if not allowed:
        return
    # Extract hostname from SSH (git@host:...) or HTTPS (https://host/...)
    m = re.match(r"(?:git@|ssh://(?:[^@]+@)?)([^:/]+)", repo_url)
    if not m:
        m = re.match(r"https?://([^/]+)", repo_url)
    hostname = m.group(1).lower() if m else ""
    if hostname not in allowed:
        raise HTTPException(
            status_code=403,
            detail=f"Clone host '{hostname}' not in WEBHOOK_ALLOWED_HOSTS",
        )


async def _enqueue_job(repo_url: str, branch: str,
                       commit_sha: str, triggered_by: str) -> str:
    async with AsyncSessionLocal() as db:
        repo_name = repo_url.rstrip("/").split("/")[-1].removesuffix(".git")
        job_id = await create_index_job(db, repo_name, branch, commit_sha, triggered_by)

    # Send to Celery worker
    index_repository_task.delay(
        repo_url=repo_url,
        branch=branch,
        commit_sha=commit_sha,
        job_id=str(job_id),
        version=branch,
    )
    return str(job_id)


def _verify_github_signature(body: bytes, signature_header: Optional[str]):
    if not signature_header:
        raise HTTPException(status_code=401, detail="Missing X-Hub-Signature-256")
    expected = "sha256=" + hmac.new(
        settings.github_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature_header):
        raise HTTPException(status_code=401, detail="Invalid signature")
