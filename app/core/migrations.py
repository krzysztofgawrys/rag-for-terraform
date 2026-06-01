"""
Lightweight SQL migration runner.

Reads numbered .sql files from the ``migrations/`` directory and executes
them in order, skipping any that have already been applied. Applied
migrations are tracked in the ``schema_migrations`` table.

Usage (async)::

    from app.core.migrations import run_migrations
    await run_migrations()          # uses default database_url from settings

The runner is safe to call on every startup — it acquires an advisory lock
so concurrent workers don't race.
"""

from pathlib import Path

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings

log = structlog.get_logger()
settings = get_settings()

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

# PostgreSQL advisory lock ID (arbitrary constant).
_LOCK_ID = 834_291_7


async def run_migrations(database_url: str | None = None) -> int:
    """Apply pending migrations. Returns the number of migrations applied."""
    url = database_url or settings.database_url
    engine = create_async_engine(url, echo=False)

    applied = 0
    try:
        async with engine.begin() as conn:
            # Advisory lock — only one process runs migrations at a time
            await conn.execute(text(f"SELECT pg_advisory_lock({_LOCK_ID})"))

            # Ensure tracking table exists
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version  TEXT PRIMARY KEY,
                    name     TEXT NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """))

            # Load already-applied versions
            result = await conn.execute(
                text("SELECT version FROM schema_migrations ORDER BY version")
            )
            done = {row[0] for row in result.fetchall()}

            # Discover migration files: 001_description.sql, 002_description.sql, ...
            if not MIGRATIONS_DIR.is_dir():
                log.warning("migrations_dir_missing", path=str(MIGRATIONS_DIR))
                return 0

            files = sorted(MIGRATIONS_DIR.glob("*.sql"))

            for f in files:
                version = f.stem.split("_", 1)[0]  # "001"
                if version in done:
                    continue

                sql = f.read_text(encoding="utf-8").strip()
                # Template variable substitution for migrations that need config values
                sql = sql.replace("{{EMBEDDING_DIM}}", str(settings.embedding_dim))
                # Skip empty migrations and comment-only migrations
                sql_no_comments = "\n".join(
                    line for line in sql.splitlines()
                    if line.strip() and not line.strip().startswith("--")
                )
                if not sql_no_comments.strip():
                    log.info("migration_skip_empty", version=version, file=f.name)
                    await conn.execute(
                        text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"),
                        {"v": version, "n": f.stem},
                    )
                    continue

                log.info("migration_applying", version=version, file=f.name)
                # asyncpg cannot execute multiple statements in one
                # prepared statement, so we use the raw DBAPI connection.
                raw = await conn.get_raw_connection()
                await raw.dbapi_connection.driver_connection.execute(sql)
                await conn.execute(
                    text("INSERT INTO schema_migrations (version, name) VALUES (:v, :n)"),
                    {"v": version, "n": f.stem},
                )
                applied += 1
                log.info("migration_applied", version=version, file=f.name)

            # Release advisory lock (released automatically on disconnect too)
            await conn.execute(text(f"SELECT pg_advisory_unlock({_LOCK_ID})"))
    finally:
        await engine.dispose()

    if applied:
        log.info("migrations_complete", applied=applied)
    else:
        log.debug("migrations_none_pending")

    return applied
