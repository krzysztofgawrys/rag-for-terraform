"""Tests for app.core.graph recursive-CTE traversal.

Exercises the real SQL against a real Postgres (the CTEs use ARRAY[], = ANY(),
||, split_part - Postgres-only, so SQLite cannot stand in).

The load-bearing cases are the cycles: get_dependency_tree() guards cycles
with a `visited` array; find_dependents() (depth>1) has NO visited set and
relies solely on the depth bound. Both must terminate. The @timeout marks
turn an infinite loop (guard regression) into a failure instead of a hang.

Run:  pytest tests/test_graph.py -v
"""
import pytest

from app.core.graph import get_dependency_tree, find_dependents

from tests.conftest import requires_db

pytestmark = requires_db


# --------------------------------------------------------------------------
# get_dependency_tree (forward: what does this module depend on?)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_linear_chain(graph_db):
    # app -> vpc -> subnet
    graph_db([("app", "vpc"), ("vpc", "subnet")])
    rows = await get_dependency_tree("app", depth=3)
    assert {r["dep_path"] for r in rows} == {"vpc", "subnet"}


@pytest.mark.asyncio
async def test_tree_respects_depth_bound(graph_db):
    # app -> vpc -> subnet, but only ask one level deep
    graph_db([("app", "vpc"), ("vpc", "subnet")])
    rows = await get_dependency_tree("app", depth=1)
    assert {r["dep_path"] for r in rows} == {"vpc"}


@pytest.mark.asyncio
async def test_branching_tree(graph_db):
    # app depends on two siblings
    graph_db([("app", "vpc"), ("app", "rds")])
    rows = await get_dependency_tree("app", depth=3)
    assert {r["dep_path"] for r in rows} == {"vpc", "rds"}


@pytest.mark.asyncio
async def test_empty_graph_returns_nothing(graph_db):
    graph_db([])
    rows = await get_dependency_tree("nonexistent", depth=5)
    assert rows == []


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_forward_cycle_terminates(graph_db):
    """a -> b -> a. The visited-array guard must stop traversal re-entering a.

    Expected: from a we reach b, then b->a is blocked because 'mods/a' is
    already in the visited set. Result is exactly {b} - and crucially the
    query returns at all.
    """
    graph_db([("a", "b"), ("b", "a")])
    rows = await get_dependency_tree("a", depth=10)
    assert {r["dep_path"] for r in rows} == {"b"}


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_forward_self_loop_terminates(graph_db):
    """a -> a (pathological self-dependency). Must not loop forever."""
    graph_db([("a", "a")])
    rows = await get_dependency_tree("a", depth=10)
    # anchor yields the a->a edge once; the visited guard blocks re-entry
    assert {r["dep_path"] for r in rows} == {"a"}


# --------------------------------------------------------------------------
# find_dependents (reverse: who depends on this module?)
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_direct_dependents(graph_db):
    # both app and api depend on vpc
    graph_db([("app", "vpc"), ("api", "vpc")])
    rows = await find_dependents("vpc", depth=1)
    assert {r["path"] for r in rows} == {"app", "api"}


@pytest.mark.asyncio
async def test_transitive_dependents(graph_db):
    # app -> vpc -> subnet : both vpc and app depend (transitively) on subnet
    graph_db([("app", "vpc"), ("vpc", "subnet")])
    rows = await find_dependents("subnet", depth=3)
    assert {r["path"] for r in rows} == {"vpc", "app"}


@pytest.mark.asyncio
async def test_direct_dependents_ignore_transitive(graph_db):
    graph_db([("app", "vpc"), ("vpc", "subnet")])
    rows = await find_dependents("subnet", depth=1)
    assert {r["path"] for r in rows} == {"vpc"}


@pytest.mark.asyncio
@pytest.mark.timeout(15)
async def test_reverse_cycle_terminates(graph_db):
    """a -> b -> a. find_dependents has no visited set - only the depth bound
    (rd.lvl < :depth) and UNION dedup keep this from looping forever.
    This is the weaker of the two guards, so it gets an explicit test.
    """
    graph_db([("a", "b"), ("b", "a")])
    rows = await find_dependents("a", depth=5)
    # who depends on a -> b; who depends on b -> a : transitive closure {a, b}
    assert {r["path"] for r in rows} == {"a", "b"}
