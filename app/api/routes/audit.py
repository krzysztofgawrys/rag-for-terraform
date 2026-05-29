"""
Audit log browser — read-only access to audit_logs table.
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from typing import Optional

from app.core.vector_store import get_db
from app.core.auth import AuthenticatedUser, require_admin

router = APIRouter(prefix="/audit", tags=["audit"])


@router.get("/")
async def list_audit_logs(
    category: Optional[str] = Query(None, description="Filter: api, mcp, worker, llm"),
    action: Optional[str] = Query(None, description="Filter by action substring"),
    status: Optional[str] = Query(None, description="Filter: success, error"),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    user: AuthenticatedUser = require_admin,
    db: AsyncSession = Depends(get_db),
):
    """Browse audit logs with optional filters, newest first."""
    conditions = ["TRUE"]
    params: dict = {"limit": limit, "offset": offset}

    if category:
        conditions.append("category = :category")
        params["category"] = category
    if action:
        conditions.append("action ILIKE :action")
        params["action"] = f"%{action}%"
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where = " AND ".join(conditions)

    count_result = await db.execute(
        text(f"SELECT COUNT(*) FROM audit_logs WHERE {where}"), params
    )
    total = count_result.scalar()

    result = await db.execute(
        text(f"""
            SELECT id, created_at, category, action, status, duration_ms,
                   request_data, response_data, error, metadata
            FROM audit_logs
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    )
    rows = [dict(r) for r in result.mappings().all()]

    # Serialize datetimes and UUIDs; redact prompt data unless show_prompts=true
    from app.core.config import get_settings
    show = get_settings().audit_log_llm_prompts
    for row in rows:
        row["id"] = str(row["id"])
        row["created_at"] = row["created_at"].isoformat() if row["created_at"] else None
        if not show and row.get("category") == "llm":
            row["request_data"] = {"redacted": True}
            row["response_data"] = {"redacted": True}

    return {"total": total, "limit": limit, "offset": offset, "items": rows}


@router.get("/stats")
async def audit_stats(user: AuthenticatedUser = require_admin, db: AsyncSession = Depends(get_db)):
    """Summary counts by category and status."""
    result = await db.execute(text("""
        SELECT category, status, COUNT(*) AS cnt
        FROM audit_logs
        GROUP BY category, status
        ORDER BY category, status
    """))
    rows = [dict(r) for r in result.mappings().all()]
    return {"breakdown": rows}
