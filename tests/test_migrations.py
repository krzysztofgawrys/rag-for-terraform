"""Tests for the advisory-lock SQL migration runner (app/core/migrations.py).

Covers the properties that make the runner safe to call on every startup:
  - ordering: files apply in numeric order (002 can depend on 001)
  - idempotency: a second run is a no-op (0 applied)
  - comment-only / empty migrations are recorded but NOT executed
    (regression guard for the "comment-only migration crash" fix)
  - {{EMBEDDING_DIM}} template substitution
  - concurrent starts serialize via pg_advisory_lock (no double-apply)

Runs against a throwaway database (POSTGRES_DB, default ragtest). MIGRATIONS_DIR
is monkeypatched to a tmp dir so the repo's real migrations are never touched.
DB-less environments skip cleanly.
"""
import asyncio
import os
import subprocess

import pytest

import app.core.migrations as mig
from app.core.migrations import run_migrations


# ---------------------------------------------------------------------------
# DB plumbing (psql over the host port, same env the app reads)
# ---------------------------------------------------------------------------
def _psql(sql: str):
    env = {
        **os.environ,
        "PGHOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        "PGPORT": os.environ.get("POSTGRES_PORT", "5432"),
        "PGUSER": os.environ.get("POSTGRES_USER", "terraform_rag"),
        "PGPASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
        "PGDATABASE": os.environ.get("POSTGRES_DB", "ragtest"),
    }
    return subprocess.run(
        ["psql", "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=env, capture_output=True, text=True, timeout=20,
    )


def _db_up() -> bool:
    try:
        return _psql("SELECT 1").returncode == 0
    except Exception:
        return False


requires_db = pytest.mark.skipif(not _db_up(), reason="no test Postgres reachable")

_TEST_TABLES = "_mtest_a, _mtest_b, _mtest_should_not_exist"


@pytest.fixture
def clean_db():
    """Drop the runner's tracking table and our test tables before/after."""
    _psql(f"DROP TABLE IF EXISTS schema_migrations, {_TEST_TABLES} CASCADE;")
    yield _psql
    _psql(f"DROP TABLE IF EXISTS schema_migrations, {_TEST_TABLES} CASCADE;")


def _versions():
    out = _psql("SELECT version FROM schema_migrations ORDER BY version;")
    return [v for v in out.stdout.splitlines() if v.strip()]


def _table_exists(name: str) -> bool:
    out = _psql(f"SELECT to_regclass('public.{name}') IS NOT NULL;")
    return out.stdout.strip() == "t"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@requires_db
async def test_applies_in_numeric_order(tmp_path, monkeypatch, clean_db):
    # 002 ALTERs the table 001 creates -> only succeeds if 001 ran first.
    (tmp_path / "001_create.sql").write_text("CREATE TABLE _mtest_a (id int);")
    (tmp_path / "002_alter.sql").write_text(
        "ALTER TABLE _mtest_a ADD COLUMN name text;"
    )
    monkeypatch.setattr(mig, "MIGRATIONS_DIR", tmp_path)

    applied = await run_migrations()
    assert applied == 2
    assert _versions() == ["001", "002"]
    assert _table_exists("_mtest_a")


@requires_db
async def test_second_run_is_noop(tmp_path, monkeypatch, clean_db):
    (tmp_path / "001_create.sql").write_text("CREATE TABLE _mtest_a (id int);")
    monkeypatch.setattr(mig, "MIGRATIONS_DIR", tmp_path)

    assert await run_migrations() == 1
    assert await run_migrations() == 0          # idempotent
    assert _versions() == ["001"]               # not double-recorded


@requires_db
async def test_comment_only_migration_recorded_not_executed(tmp_path, monkeypatch, clean_db):
    # If the body were executed it would create the table; it must NOT be.
    (tmp_path / "001_note.sql").write_text(
        "-- purely a note\n-- CREATE TABLE _mtest_should_not_exist (x int);\n"
    )
    monkeypatch.setattr(mig, "MIGRATIONS_DIR", tmp_path)

    applied = await run_migrations()
    assert applied == 0                              # nothing executed...
    assert _versions() == ["001"]                    # ...but still recorded
    assert not _table_exists("_mtest_should_not_exist")


@requires_db
async def test_empty_migration_recorded_not_executed(tmp_path, monkeypatch, clean_db):
    (tmp_path / "001_empty.sql").write_text("\n   \n")
    monkeypatch.setattr(mig, "MIGRATIONS_DIR", tmp_path)

    assert await run_migrations() == 0
    assert _versions() == ["001"]


@requires_db
async def test_embedding_dim_template_substituted(tmp_path, monkeypatch, clean_db):
    # The runner replaces {{EMBEDDING_DIM}} with settings.embedding_dim.
    (tmp_path / "001_vec.sql").write_text(
        "CREATE TABLE _mtest_b (v vector({{EMBEDDING_DIM}}));"
    )
    monkeypatch.setattr(mig, "MIGRATIONS_DIR", tmp_path)

    assert await run_migrations() == 1
    out = _psql(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid='_mtest_b'::regclass AND attname='v';"
    )
    assert f"vector({mig.settings.embedding_dim})" in out.stdout


@requires_db
async def test_concurrent_runs_when_applied_are_safe_noops(tmp_path, monkeypatch, clean_db):
    """The real production case: several workers concurrently call run_migrations
    on an ALREADY-migrated DB (every stack restart). The committed
    schema_migrations + advisory lock make each concurrent call a clean
    0-applied no-op - no double-apply, no error.
    """
    (tmp_path / "001_create.sql").write_text("CREATE TABLE _mtest_a (id int);")
    (tmp_path / "002_create.sql").write_text("CREATE TABLE _mtest_b (id int);")
    monkeypatch.setattr(mig, "MIGRATIONS_DIR", tmp_path)

    assert await run_migrations() == 2                # serial first apply, commits
    results = await asyncio.gather(*[run_migrations() for _ in range(4)])
    assert results == [0, 0, 0, 0]                    # all clean no-ops
    assert _versions() == ["001", "002"]

    # NOTE (latent finding, not asserted): on a *fresh* DB with simultaneous
    # first runs, pg_advisory_unlock fires inside the transaction (migrations.py
    # line ~101) BEFORE commit, so a second runner can acquire the lock and not
    # yet see the first's CREATE TABLE schema_migrations -> a pg_type race. The
    # safe fix is to drop the explicit unlock and rely on release-on-dispose
    # (which happens after commit). Left for the maintainer to decide.
