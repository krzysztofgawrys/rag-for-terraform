"""Shared fixtures for DB-backed tests (graph recursive CTEs).

Connection comes from POSTGRES_* env vars (same ones the app reads via
pydantic-settings). These MUST be set before app.core.* is imported, so they
are applied here at module top - conftest.py is loaded before test modules.

If no test Postgres is reachable, the DB tests skip cleanly rather than fail.
Point at any throwaway Postgres:

    POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5433 POSTGRES_USER=postgres \
    POSTGRES_PASSWORD= POSTGRES_DB=ragtest pytest tests/test_graph.py -v
"""
import os

os.environ.setdefault("POSTGRES_USER", "postgres")
os.environ.setdefault("POSTGRES_PASSWORD", "")
os.environ.setdefault("POSTGRES_HOST", "127.0.0.1")
os.environ.setdefault("POSTGRES_PORT", "5433")
os.environ.setdefault("POSTGRES_DB", "ragtest")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-not-used")

import asyncio
import subprocess

import pytest

PG = {
    "host": os.environ["POSTGRES_HOST"],
    "port": os.environ["POSTGRES_PORT"],
    "user": os.environ["POSTGRES_USER"],
    "db": os.environ["POSTGRES_DB"],
}

# Minimal schema: the real module_dependencies DDL (migration 005) plus a
# stand-in `modules` table covering only the columns the CTEs LEFT JOIN on.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS modules (
    repo         TEXT,
    module_path  TEXT,
    version      TEXT,
    module_name  TEXT
);
CREATE TABLE IF NOT EXISTS module_dependencies (
    parent_repo     TEXT NOT NULL,
    parent_path     TEXT NOT NULL,
    parent_version  TEXT NOT NULL,
    dep_repo        TEXT NOT NULL,
    dep_path        TEXT NOT NULL,
    dep_version     TEXT NOT NULL,
    dep_name        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (parent_repo, parent_path, parent_version,
                 dep_repo, dep_path, dep_version)
);
"""


def _psql(sql: str):
    """Run SQL via psql. Returns CompletedProcess; raises on connection failure."""
    return subprocess.run(
        ["psql", "-h", PG["host"], "-p", PG["port"], "-U", PG["user"],
         "-d", PG["db"], "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=15,
    )


def _db_available() -> bool:
    try:
        return _psql("SELECT 1;").returncode == 0
    except Exception:
        return False


DB_AVAILABLE = _db_available()
requires_db = pytest.mark.skipif(not DB_AVAILABLE, reason="no test Postgres reachable")


@pytest.fixture(scope="session", autouse=True)
def _schema():
    if DB_AVAILABLE:
        _psql(SCHEMA_SQL)
    yield


if DB_AVAILABLE:
    # The app builds its async engine at import time with a connection pool.
    # pytest-asyncio runs each test in its own event loop, and a pooled asyncpg
    # connection bound to loop A blows up when reused on loop B
    # ("Future attached to a different loop"). Rebinding to a NullPool engine
    # means every operation opens a fresh connection on the *current* loop, so
    # tests are loop-independent. (The app itself solves the same problem with
    # make_session_factory() in Celery workers.)
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool
    from app.core.config import get_settings
    import app.core.vector_store as _vs
    import app.core.graph as _graph

    _test_engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    _test_session = sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)
    _vs.AsyncSessionLocal = _test_session
    _graph.AsyncSessionLocal = _test_session  # graph imported it by value


@pytest.fixture
def graph_db():
    """Truncate the dependency table, then hand back a seed() helper.

    seed(edges, repo='mods', version='v1') inserts (parent_path -> dep_path)
    edges. dep_name defaults to the dep_path.
    """
    _psql("TRUNCATE module_dependencies; TRUNCATE modules;")

    def seed(edges, repo="mods", version="v1"):
        if not edges:
            return
        values = ",".join(
            f"('{repo}','{p}','{version}','{repo}','{d}','{version}','{d}')"
            for p, d in edges
        )
        _psql(
            "INSERT INTO module_dependencies "
            "(parent_repo,parent_path,parent_version,dep_repo,dep_path,dep_version,dep_name) "
            f"VALUES {values};"
        )

    return seed
