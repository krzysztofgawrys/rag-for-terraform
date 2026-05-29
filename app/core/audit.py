"""
Audit logging — single module for all categories (api, mcp, worker, llm).

Usage:
    from app.core.audit import emit
    await emit("api", "POST /query/", request_data={...}, response_data={...}, duration_ms=42)

Non-blocking: DB writes happen via fire-and-forget asyncio tasks.
Structlog output is synchronous (goes to console immediately).
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import time
import traceback
from typing import Any

import structlog

from app.core.config import get_settings

log = structlog.get_logger("audit")

_settings = None


def _get_settings():
    global _settings
    if _settings is None:
        _settings = get_settings()
    return _settings


def _get_user_context() -> tuple[str | None, str | None]:
    """Read user_id / user_email from structlog contextvars (set by auth layer)."""
    ctx = structlog.contextvars.get_contextvars()
    return ctx.get("user_id"), ctx.get("user_email")


async def emit(
    category: str,
    action: str,
    *,
    status: str = "success",
    duration_ms: int | None = None,
    request_data: Any = None,
    response_data: Any = None,
    error: str | None = None,
    metadata: dict | None = None,
    user_id: str | None = None,
    user_email: str | None = None,
) -> None:
    """Log an audit event to structlog + PostgreSQL (non-blocking)."""
    settings = _get_settings()
    if not settings.audit_log_enabled:
        return

    # Auto-fill user context from structlog contextvars if not explicitly passed
    if user_id is None or user_email is None:
        ctx_uid, ctx_email = _get_user_context()
        user_id = user_id or ctx_uid
        user_email = user_email or ctx_email

    req_data = request_data
    resp_data = response_data
    if category == "llm" and not settings.audit_log_llm_prompts:
        req_data = {"redacted": True, "prompt_length": len(str(request_data or ""))}
        resp_data = {"redacted": True, "response_length": len(str(response_data or ""))}

    log.info(
        "audit_event",
        category=category,
        action=action,
        status=status,
        duration_ms=duration_ms,
        error=error,
        metadata=metadata,
        user_email=user_email,
    )

    asyncio.create_task(_write_to_db(
        category=category,
        action=action,
        status=status,
        duration_ms=duration_ms,
        request_data=req_data,
        response_data=resp_data,
        error=error,
        metadata=metadata or {},
        user_id=user_id,
        user_email=user_email,
    ))


def emit_sync(
    category: str,
    action: str,
    **kwargs: Any,
) -> None:
    """Synchronous variant for Celery tasks and other sync contexts."""
    settings = _get_settings()
    if not settings.audit_log_enabled:
        return

    req_data = kwargs.get("request_data")
    resp_data = kwargs.get("response_data")
    if category == "llm" and not settings.audit_log_llm_prompts:
        req_data = {"redacted": True, "prompt_length": len(str(req_data or ""))}
        resp_data = {"redacted": True, "response_length": len(str(resp_data or ""))}

    # User context — explicit kwargs or from contextvars
    uid = kwargs.get("user_id")
    uemail = kwargs.get("user_email")
    if uid is None or uemail is None:
        ctx_uid, ctx_email = _get_user_context()
        uid = uid or ctx_uid
        uemail = uemail or ctx_email

    log.info(
        "audit_event",
        category=category,
        action=action,
        status=kwargs.get("status", "success"),
        duration_ms=kwargs.get("duration_ms"),
        error=kwargs.get("error"),
        metadata=kwargs.get("metadata"),
        user_email=uemail,
    )

    db_kwargs = dict(
        category=category, action=action,
        status=kwargs.get("status", "success"),
        duration_ms=kwargs.get("duration_ms"),
        request_data=req_data,
        response_data=resp_data,
        error=kwargs.get("error"),
        metadata=kwargs.get("metadata") or {},
        user_id=uid,
        user_email=uemail,
    )

    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_write_to_db(**db_kwargs))
    except RuntimeError:
        # No running loop (e.g. Celery finally block after asyncio.run finished).
        # Spin up a throwaway loop for the DB write.
        try:
            asyncio.run(_write_to_db(**db_kwargs))
        except Exception:
            pass  # structlog already fired; DB write is best-effort


# -- DB writer ----------------------------------------------------------------

# Per-event-loop engine cache: engines are bound to a single loop, so we key
# by id(loop) and lazy-create. In Celery (no running loop) we fall back to
# a one-shot engine per call.
_engine_cache: dict[int, Any] = {}
_session_cache: dict[int, Any] = {}


def _get_or_create_engine() -> tuple[Any, Any, bool]:
    """Return (Session, engine, is_ephemeral) for the current loop.

    is_ephemeral=True means caller must `await engine.dispose()` after use
    (no running loop, e.g. inside asyncio.run() in a Celery task).
    """
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker

    settings = _get_settings()

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        key = id(loop)
        cached_engine = _engine_cache.get(key)
        if cached_engine is None:
            cached_engine = create_async_engine(
                settings.database_url,
                pool_size=2,
                max_overflow=3,
                pool_pre_ping=True,
                pool_recycle=1800,
            )
            _engine_cache[key] = cached_engine
            _session_cache[key] = sessionmaker(
                cached_engine, class_=AsyncSession, expire_on_commit=False
            )
        return _session_cache[key], cached_engine, False

    # No running loop — caller is inside asyncio.run() (Celery). Build a
    # one-shot engine the caller must dispose.
    engine = create_async_engine(settings.database_url, pool_size=1)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return Session, engine, True


async def _write_to_db(*, category, action, status, duration_ms,
                       request_data, response_data, error, metadata,
                       user_id=None, user_email=None):
    """Insert one row into audit_logs. Swallows all exceptions.

    Reuses a per-event-loop engine for performance (was creating new engine
    per call, which connects + auths to Postgres every time).
    """
    ephemeral_engine = None
    try:
        from sqlalchemy import text as sa_text

        Session, engine, is_ephemeral = _get_or_create_engine()
        if is_ephemeral:
            ephemeral_engine = engine

        def _jsonable(obj):
            if obj is None:
                return None
            try:
                json.dumps(obj)
                return obj
            except (TypeError, ValueError):
                return str(obj)

        try:
            async with Session() as db:
                await db.execute(
                    sa_text("""
                        INSERT INTO audit_logs
                            (category, action, status, duration_ms,
                             request_data, response_data, error, metadata,
                             user_id, user_email)
                        VALUES
                            (:category, :action, :status, :duration_ms,
                             CAST(:request_data AS jsonb), CAST(:response_data AS jsonb),
                             :error, CAST(:metadata AS jsonb),
                             CAST(:user_id AS uuid), :user_email)
                    """),
                    {
                        "category": category,
                        "action": action,
                        "status": status,
                        "duration_ms": duration_ms,
                        "request_data": json.dumps(_jsonable(request_data)),
                        "response_data": json.dumps(_jsonable(response_data)),
                        "error": error,
                        "metadata": json.dumps(metadata),
                        "user_id": user_id,
                        "user_email": user_email,
                    },
                )
                await db.commit()
        finally:
            # Only dispose ephemeral engines (Celery one-shot loops).
            # Cached engines stay alive for the lifetime of the event loop.
            if ephemeral_engine is not None:
                await ephemeral_engine.dispose()
    except Exception:
        log.error("audit_db_write_failed", category=category, action=action, error=traceback.format_exc())


# -- MCP tool decorator -------------------------------------------------------

def audit_mcp_tool(func):
    """Decorator for MCP tool functions. Logs invocation, args, result, duration."""
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        start = time.perf_counter()
        tool_name = func.__name__
        error_text = None
        result = None
        try:
            result = await func(*args, **kwargs)
            return result
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            await emit(
                "mcp", f"tool:{tool_name}",
                status="error" if error_text else "success",
                duration_ms=int((time.perf_counter() - start) * 1000),
                request_data=kwargs or None,
                response_data={"response_length": len(result)} if result and not error_text else None,
                error=error_text,
                metadata={"tool": tool_name},
            )

    # Preserve original signature for FastMCP parameter introspection
    wrapper.__signature__ = inspect.signature(func)
    return wrapper
