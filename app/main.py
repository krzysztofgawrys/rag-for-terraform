import logging

import structlog
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core import graph as graph_db
from app.core.migrations import run_migrations
from app.api.routes import webhook, index, query, modules, consumer, snippets, audit
from app.api.routes import auth as auth_routes
from app.api import mcp_tools
from app.core.auth import AuthMiddleware, seed_initial_admin, seed_demo_user, seed_mcp_api_key, seed_ci_api_key

settings = get_settings()

# -- Structlog configuration --------------------------------------------------
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer() if settings.debug else structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG if settings.debug else logging.INFO),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger()

mcp_http_app = mcp_tools.mcp.streamable_http_app()


async def _reap_stale_jobs() -> None:
    """Mark stale 'running' jobs as failed.

    A job stays 'running' in the DB while its Celery task is in flight.
    If the worker crashes or is restarted (docker compose restart worker),
    Celery with acks_late=False has already acked the task and won't
    redeliver it — the DB row is left in 'running' forever with stale stats.

    On API startup we reap any job whose started_at is older than 1 hour
    AND has no recent stats update — those are guaranteed-dead tasks.
    """
    from sqlalchemy import text
    from app.core.vector_store import make_session_factory
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            # Index jobs (full-repo indexing)
            r1 = await db.execute(text("""
                UPDATE index_jobs
                SET status = 'failed',
                    finished_at = NOW(),
                    error = COALESCE(error, '') ||
                            ' [auto-reaped: worker restarted before completion]'
                WHERE status = 'running'
                  AND started_at < NOW() - INTERVAL '1 hour'
            """))
            # Consumer index jobs (knowledge layer)
            r2 = await db.execute(text("""
                UPDATE consumer_index_jobs
                SET status = 'failed',
                    finished_at = NOW(),
                    error = COALESCE(error, '') ||
                            ' [auto-reaped: worker restarted before completion]'
                WHERE status = 'running'
                  AND started_at < NOW() - INTERVAL '1 hour'
            """))
            await db.commit()
            reaped = (r1.rowcount or 0) + (r2.rowcount or 0)
            if reaped:
                log.warning("stale_jobs_reaped", count=reaped)
    finally:
        await engine.dispose()


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("startup", app=settings.app_name)
    await run_migrations()
    await graph_db.init_constraints()
    await _reap_stale_jobs()
    await seed_initial_admin()
    await seed_demo_user()
    await seed_mcp_api_key()
    await seed_ci_api_key()
    async with mcp_http_app.router.lifespan_context(mcp_http_app):
        yield
    await graph_db.close_driver()
    log.info("shutdown")


app = FastAPI(
    title=settings.app_name,
    description=(
        "RAG backend for Terraform module knowledge base. "
        "Indexes repositories, tracks dependencies, and generates/optimizes Terraform code."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

_cors_origins = (
    [o.strip() for o in settings.frontend_url.split(",") if o.strip()]
    if settings.auth_mode != "disabled"
    else ["*"]
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=settings.auth_mode != "disabled",
    allow_methods=["*"],
    allow_headers=["*"],
)

# -- Routes --------------------------------------------------------------------
app.include_router(auth_routes.router)
app.include_router(webhook.router)
app.include_router(index.router)
app.include_router(query.router)
app.include_router(modules.router)
app.include_router(consumer.router)
app.include_router(snippets.router)
app.include_router(audit.router)


@app.get("/health")
async def health():
    return {"status": "ok", "version": "1.0.0"}


# MCP mount: when Cognito OAuth is active, FastMCP handles auth natively
# via BearerAuthBackend + RequireAuthMiddleware. Otherwise fall back to
# our custom AuthMiddleware for API key + ALB SSO validation.
_mcp_oauth_active = (
    settings.auth_mode == "sso"
    and settings.cognito_user_pool_id
    and settings.mcp_oauth_issuer_url
)
if _mcp_oauth_active:
    app.mount("", mcp_http_app)
elif settings.auth_mode != "disabled":
    app.mount("", AuthMiddleware(mcp_http_app, min_role="readonly"))
else:
    app.mount("", mcp_http_app)
