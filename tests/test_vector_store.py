"""Tests for vector_store.similarity_search with INJECTED deterministic vectors.

This is the core retrieval primitive. Two properties matter most and are easy to
get subtly wrong:
  - cosine ranking: nearest embedding ranks first
  - "latest version wins": with version_filter=None the result carries the newest
    semver per (repo, module_path), NOT whichever version had the closest vector
    (an old v1.0.0 tag must not shadow the maintained release)
plus the repo / tag / version filters and top_k.

Uses a self-contained `modules` table (vector(3)) in the throwaway DB so the
distances are exact and obvious. Skips cleanly without Postgres+pgvector.
"""
import os
import subprocess

import pytest

from app.core.vector_store import similarity_search, find_by_code_hash, AsyncSessionLocal


# ---------------------------------------------------------------------------
# DB plumbing
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


def _pgvector_ready() -> bool:
    try:
        r = _psql("CREATE EXTENSION IF NOT EXISTS vector; SELECT 1;")
        return r.returncode == 0
    except Exception:
        return False


requires_vec = pytest.mark.skipif(
    not _pgvector_ready(), reason="no test Postgres with pgvector"
)


@pytest.fixture
def modules_table():
    """A fresh vector(3) `modules` table covering the columns similarity_search
    reads. Returns an insert(repo, path, version, vec, tags) helper."""
    _psql("DROP TABLE IF EXISTS modules CASCADE;")
    _psql("""
        CREATE TABLE modules (
            id serial PRIMARY KEY,
            repo text, module_name text, module_path text, version text,
            tags text[], variables text, outputs text, resources text,
            description text, code_hash text, embedding vector(3),
            UNIQUE (repo, module_path, version)
        );
    """)

    def insert(repo, path, version, vec, tags=None, description=None, code_hash=None):
        tag_sql = (
            "ARRAY[" + ",".join(f"'{t}'" for t in tags) + "]::text[]"
            if tags else "NULL"
        )
        v = "[" + ",".join(str(x) for x in vec) + "]"
        desc_sql = "NULL" if description is None else "'" + description.replace("'", "''") + "'"
        hash_sql = "NULL" if code_hash is None else "'" + code_hash + "'"
        r = _psql(
            f"INSERT INTO modules (repo, module_name, module_path, version, tags, embedding, description, code_hash) "
            f"VALUES ('{repo}','{path.split('/')[-1]}','{path}','{version}',{tag_sql},'{v}',{desc_sql},{hash_sql});"
        )
        assert r.returncode == 0, r.stderr
    yield insert
    _psql("DROP TABLE IF EXISTS modules CASCADE;")


async def _search(query_vec, **kw):
    async with AsyncSessionLocal() as db:
        return await similarity_search(db, query_vec, **kw)


def _refs(results):
    return [f"{r['repo']}/{r['module_path']}" for r in results]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
@requires_vec
async def test_ranks_nearest_embedding_first(modules_table):
    modules_table("r", "x", "1.0.0", [1, 0, 0])       # identical to query -> nearest
    modules_table("r", "z", "1.0.0", [0.9, 0.1, 0])   # close
    modules_table("r", "y", "1.0.0", [0, 1, 0])       # orthogonal -> farthest
    results = await _search([1, 0, 0], top_k=3)
    assert _refs(results) == ["r/x", "r/z", "r/y"]
    # similarity is monotonically non-increasing
    sims = [r["similarity"] for r in results]
    assert sims == sorted(sims, reverse=True)


@requires_vec
async def test_latest_semver_version_wins_not_closest(modules_table):
    # v1.0.0 sits exactly on the query; v2.0.0 is orthogonal (far). The default
    # (version_filter=None) must still surface v2.0.0 - the maintained release.
    modules_table("r", "mod", "1.0.0", [1, 0, 0])
    modules_table("r", "mod", "2.0.0", [0, 1, 0])
    results = await _search([1, 0, 0], top_k=5)
    assert len(results) == 1                       # collapsed to one row per module
    assert results[0]["version"] == "2.0.0"        # latest, despite v1 being closer


@requires_vec
async def test_version_filter_star_returns_all_versions(modules_table):
    modules_table("r", "mod", "1.0.0", [1, 0, 0])
    modules_table("r", "mod", "2.0.0", [0, 1, 0])
    results = await _search([1, 0, 0], top_k=5, version_filter="*")
    assert {r["version"] for r in results} == {"1.0.0", "2.0.0"}


@requires_vec
async def test_repo_filter_restricts_results(modules_table):
    modules_table("alpha", "x", "1.0.0", [1, 0, 0])
    modules_table("beta", "y", "1.0.0", [1, 0, 0])
    results = await _search([1, 0, 0], top_k=5, repo_filter="alpha")
    assert _refs(results) == ["alpha/x"]


@requires_vec
async def test_tag_filter_matches_any(modules_table):
    modules_table("r", "x", "1.0.0", [1, 0, 0], tags=["networking", "vpc"])
    modules_table("r", "y", "1.0.0", [1, 0, 0], tags=["storage"])
    results = await _search([1, 0, 0], top_k=5, tag_filter=["vpc"])
    assert _refs(results) == ["r/x"]


@requires_vec
async def test_top_k_limits_row_count(modules_table):
    for i in range(5):
        modules_table("r", f"m{i}", "1.0.0", [1, 0, 0])
    results = await _search([1, 0, 0], top_k=2)
    assert len(results) == 2


# ---------------------------------------------------------------------------
# find_by_code_hash - the indexer's "skip unchanged code" cache primitive.
# Same code hash -> reuse stored description+embedding; changed hash -> miss.
# ---------------------------------------------------------------------------
async def _find(repo, path, code_hash):
    async with AsyncSessionLocal() as db:
        return await find_by_code_hash(db, repo, path, code_hash)


@requires_vec
async def test_code_hash_hit_returns_description_and_embedding(modules_table):
    modules_table("r", "x", "1.0.0", [1, 0, 0], description="an s3 bucket", code_hash="abc123")
    hit = await _find("r", "x", "abc123")
    assert hit is not None
    assert hit["description"] == "an s3 bucket"
    assert hit["embedding_str"].startswith("[")    # vector as text, ready to reuse


@requires_vec
async def test_code_hash_changed_is_a_miss(modules_table):
    # Changed code -> different hash -> no cache hit -> indexer re-embeds.
    modules_table("r", "x", "1.0.0", [1, 0, 0], description="d", code_hash="oldhash")
    assert await _find("r", "x", "newhash") is None


@requires_vec
async def test_code_hash_empty_description_not_reused(modules_table):
    # A blank description must not be served from cache.
    modules_table("r", "x", "1.0.0", [1, 0, 0], description="", code_hash="h")
    assert await _find("r", "x", "h") is None


@requires_vec
async def test_code_hash_scoped_to_repo_and_path(modules_table):
    # Identical hash under a different module is not a hit for (r, y).
    modules_table("r", "x", "1.0.0", [1, 0, 0], description="d", code_hash="shared")
    assert await _find("r", "y", "shared") is None
