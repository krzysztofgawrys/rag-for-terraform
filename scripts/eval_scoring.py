"""Pure scoring core for the retrieval eval harness.

Extracted from eval_retrieval.run_query so the match logic can be unit-tested
without standing up a server or a populated knowledge base. The HTTP harness
calls score_case() after it has turned the API response into a flat
returned_refs list.

Extensions over the original inline matching:

  forbidden_refs  - refs that MUST NOT appear (in the considered window). Any
                    present forbidden ref fails the case regardless of expected
                    matches. This makes "deprecated module must not outrank the
                    current one" expressible - the testable form of the
                    popularity-over-correctness risk.

  top_rank        - tighten the window: expected refs must appear within the
                    first `top_rank` positions, not merely somewhere in top_k.
                    Without it, a query with top_k=5 "passes" even when the
                    right module is ranked 5th behind four wrong ones. This is
                    what makes the eval sensitive to disambiguation quality
                    among near-duplicate modules.

  reciprocal_rank - 1/rank of the first matched expected ref over the FULL
                    returned list (0.0 if absent). Aggregated -> MRR, a
                    rank-aware companion to the binary hit-rate.

Backward compatible: a case with no forbidden_refs and top_rank=None scores
exactly as the original harness did.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class QueryCase:
    query: str
    query_type: str = "compose"
    expected_refs: list[str] = field(default_factory=list)
    forbidden_refs: list[str] = field(default_factory=list)
    top_k: int = 5
    top_rank: int | None = None          # None -> consider the whole returned list
    match: str = "any"                    # "any" or "all" (applies to expected_refs)
    description: str = ""


@dataclass
class ScoreResult:
    hit: bool
    matched_refs: list[str]
    missed_refs: list[str]
    forbidden_hits: list[str]             # forbidden refs that wrongly appeared
    first_match_rank: int | None          # 1-indexed rank in full list, None if absent
    reciprocal_rank: float                # 1/first_match_rank, else 0.0


def score_case(case: QueryCase, returned_refs: list[str]) -> ScoreResult:
    """Score one query's returned refs against the case. Pure function.

    `returned_refs` is the ordered list of 'repo/module_path' the system
    returned (best-first).
    """
    # Window the expected/forbidden checks; rank/MRR always use the full list.
    window = returned_refs[: case.top_rank] if case.top_rank else returned_refs

    matched = [r for r in case.expected_refs if r in window]
    missed = [r for r in case.expected_refs if r not in window]
    forbidden_hits = [r for r in case.forbidden_refs if r in window]

    if case.match == "all":
        expected_ok = len(missed) == 0 and len(case.expected_refs) > 0
    else:
        expected_ok = len(matched) > 0

    forbidden_ok = len(forbidden_hits) == 0
    hit = expected_ok and forbidden_ok

    # Rank of the first expected ref in the FULL ordered list (not the window).
    first_rank: int | None = None
    for i, ref in enumerate(returned_refs, start=1):
        if ref in case.expected_refs:
            first_rank = i
            break
    rr = (1.0 / first_rank) if first_rank else 0.0

    return ScoreResult(
        hit=hit,
        matched_refs=matched,
        missed_refs=missed,
        forbidden_hits=forbidden_hits,
        first_match_rank=first_rank,
        reciprocal_rank=rr,
    )


def case_from_entry(entry: dict, index: int = 0) -> QueryCase:
    """Build a QueryCase from a parsed YAML fixture entry, validating shape.

    Accepts the original fields plus forbidden_refs and top_rank. A case must
    assert *something*: at least one of expected_refs / forbidden_refs.
    """
    if "query" not in entry:
        raise ValueError(f"Entry {index} missing 'query'")
    expected = entry.get("expected_refs") or []
    forbidden = entry.get("forbidden_refs") or []
    if not expected and not forbidden:
        raise ValueError(f"Entry {index} must have expected_refs and/or forbidden_refs")
    match = entry.get("match", "any")
    if match not in ("any", "all"):
        raise ValueError(f"Entry {index} has invalid match={match!r} (use 'any'/'all')")

    return QueryCase(
        query=entry["query"],
        query_type=entry.get("query_type", "compose"),
        expected_refs=list(expected),
        forbidden_refs=list(forbidden),
        top_k=entry.get("top_k", 5),
        top_rank=entry.get("top_rank"),
        match=match,
        description=entry.get("description", ""),
    )


def mean_reciprocal_rank(results: list[ScoreResult]) -> float:
    """Aggregate MRR across scored cases (0.0 for an empty list)."""
    if not results:
        return 0.0
    return sum(r.reciprocal_rank for r in results) / len(results)
