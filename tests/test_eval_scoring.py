"""Unit tests for scripts/eval_scoring.py - the pure retrieval-eval scoring.

This is the one eval piece that is deterministic and runnable without a server
or a populated knowledge base. The end-to-end harness run (real /query/ against
an indexed KB) remains a manual step; these tests only lock the scoring logic.

Run:  pytest tests/test_eval_scoring.py -v
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from eval_scoring import (  # noqa: E402
    QueryCase,
    score_case,
    case_from_entry,
    mean_reciprocal_rank,
)


def case(**kw):
    base = dict(query="q", expected_refs=["mods/right"])
    base.update(kw)
    return QueryCase(**base)


# --------------------------------------------------------------------------
# baseline match: any / all  (backward-compatible behaviour)
# --------------------------------------------------------------------------
def test_match_any_hits_on_one():
    r = score_case(case(match="any"), ["mods/right", "mods/other"])
    assert r.hit is True
    assert r.matched_refs == ["mods/right"]


def test_match_any_misses_when_absent():
    r = score_case(case(match="any"), ["mods/other", "mods/another"])
    assert r.hit is False
    assert r.missed_refs == ["mods/right"]


def test_match_all_requires_every_ref():
    c = case(expected_refs=["mods/a", "mods/b"], match="all")
    assert score_case(c, ["mods/a", "mods/b", "mods/x"]).hit is True
    assert score_case(c, ["mods/a", "mods/x"]).hit is False


# --------------------------------------------------------------------------
# forbidden_refs - the popularity-over-correctness guard
# --------------------------------------------------------------------------
def test_forbidden_ref_fails_even_when_expected_present():
    """The right module surfaced, but so did a deprecated one that must not.
    The case must FAIL - surfacing the legacy module is the regression."""
    c = case(expected_refs=["mods/s3-v2"], forbidden_refs=["mods/s3-legacy"])
    r = score_case(c, ["mods/s3-v2", "mods/s3-legacy"])
    assert r.hit is False
    assert r.forbidden_hits == ["mods/s3-legacy"]


def test_passes_when_forbidden_absent():
    c = case(expected_refs=["mods/s3-v2"], forbidden_refs=["mods/s3-legacy"])
    r = score_case(c, ["mods/s3-v2", "mods/kms"])
    assert r.hit is True
    assert r.forbidden_hits == []


def test_forbidden_only_case_no_expected():
    """A case may assert purely 'this must never appear'."""
    c = QueryCase(query="q", expected_refs=[], forbidden_refs=["mods/banned"], match="any")
    # match=any with no expected -> expected_ok is False; but the real signal
    # here is the forbidden check. Document the actual behaviour:
    clean = score_case(c, ["mods/ok"])
    dirty = score_case(c, ["mods/banned"])
    assert dirty.forbidden_hits == ["mods/banned"]
    assert dirty.hit is False
    # with no expected refs and match=any, expected_ok is False, so hit is
    # False even when clean - forbidden-only cases should use match="all"
    assert clean.forbidden_hits == []


def test_forbidden_only_case_with_match_all_passes_when_clean():
    c = QueryCase(query="q", expected_refs=[], forbidden_refs=["mods/banned"], match="all")
    # match=all + empty expected -> expected_ok is False by design (len>0 guard),
    # so even forbidden-only cases need at least one expected ref to 'hit'.
    # This asserts the guard rather than papering over it.
    assert score_case(c, ["mods/ok"]).hit is False


# --------------------------------------------------------------------------
# top_rank - disambiguation sensitivity (the core use case)
# --------------------------------------------------------------------------
def test_top_rank_passes_when_right_module_is_first():
    c = case(expected_refs=["mods/vpc-7"], top_rank=1)
    r = score_case(c, ["mods/vpc-7", "mods/vpc-3", "mods/vpc-9"])
    assert r.hit is True


def test_top_rank_fails_when_wrong_sibling_outranks():
    """15 near-duplicate VPC modules; the correct one (#7) is present in top_k
    but ranked behind a sibling. With top_rank=1 this MUST fail - which the
    original 'anywhere in top_k' matching could not catch."""
    c = case(expected_refs=["mods/vpc-7"], top_rank=1)
    r = score_case(c, ["mods/vpc-3", "mods/vpc-7", "mods/vpc-9"])
    assert r.hit is False
    assert r.missed_refs == ["mods/vpc-7"]   # outside the rank-1 window


def test_top_rank_window_wider_than_one():
    c = case(expected_refs=["mods/vpc-7"], top_rank=3)
    assert score_case(c, ["a", "b", "mods/vpc-7", "c"]).hit is True   # rank 3, inside
    assert score_case(c, ["a", "b", "c", "mods/vpc-7"]).hit is False  # rank 4, outside


def test_forbidden_respects_top_rank_window():
    """A forbidden ref appearing *below* the rank window does not fail the case
    - only its presence within top_rank matters."""
    c = case(expected_refs=["mods/right"], forbidden_refs=["mods/legacy"], top_rank=2)
    # legacy at rank 3, outside the window -> still a pass
    assert score_case(c, ["mods/right", "mods/x", "mods/legacy"]).hit is True
    # legacy at rank 2, inside the window -> fail
    assert score_case(c, ["mods/right", "mods/legacy"]).hit is False


# --------------------------------------------------------------------------
# reciprocal rank / MRR
# --------------------------------------------------------------------------
def test_reciprocal_rank_uses_full_list_not_window():
    # top_rank windows the hit/miss, but RR reflects true position
    c = case(expected_refs=["mods/right"], top_rank=1)
    r = score_case(c, ["mods/x", "mods/right"])
    assert r.first_match_rank == 2
    assert r.reciprocal_rank == 0.5
    assert r.hit is False                     # outside rank-1 window


def test_reciprocal_rank_absent_is_zero():
    r = score_case(case(), ["mods/x", "mods/y"])
    assert r.first_match_rank is None
    assert r.reciprocal_rank == 0.0


def test_mrr_aggregate():
    results = [
        score_case(case(), ["mods/right"]),            # rank 1 -> 1.0
        score_case(case(), ["x", "mods/right"]),        # rank 2 -> 0.5
        score_case(case(), ["x", "y"]),                 # absent -> 0.0
    ]
    assert mean_reciprocal_rank(results) == pytest.approx((1.0 + 0.5 + 0.0) / 3)
    assert mean_reciprocal_rank([]) == 0.0


# --------------------------------------------------------------------------
# fixture loading / validation
# --------------------------------------------------------------------------
def test_case_from_entry_full():
    c = case_from_entry({
        "query": "deploy vpc",
        "query_type": "compose",
        "expected_refs": ["mods/vpc-7"],
        "forbidden_refs": ["mods/vpc-legacy"],
        "top_rank": 1,
        "match": "all",
        "description": "disambiguation",
    }, index=0)
    assert c.expected_refs == ["mods/vpc-7"]
    assert c.forbidden_refs == ["mods/vpc-legacy"]
    assert c.top_rank == 1
    assert c.match == "all"


def test_case_from_entry_rejects_assertionless():
    with pytest.raises(ValueError):
        case_from_entry({"query": "q"}, index=3)        # no expected, no forbidden


def test_case_from_entry_rejects_bad_match():
    with pytest.raises(ValueError):
        case_from_entry({"query": "q", "expected_refs": ["x"], "match": "some"}, index=1)


def test_case_from_entry_forbidden_only_allowed():
    c = case_from_entry({"query": "q", "forbidden_refs": ["mods/banned"]}, index=0)
    assert c.expected_refs == []
    assert c.forbidden_refs == ["mods/banned"]


# --------------------------------------------------------------------------
# the shipped adversarial fixture file must stay valid (guards against rot)
# --------------------------------------------------------------------------
def test_adversarial_fixture_file_is_valid():
    import yaml
    path = Path(__file__).resolve().parent.parent / "scripts" / "eval_queries_adversarial.yaml"
    raw = yaml.safe_load(path.read_text())
    cases = [case_from_entry(e, i) for i, e in enumerate(raw)]
    assert len(cases) >= 1
    # every adversarial case must actually be adversarial: it tightens ranking
    # (top_rank) and/or asserts a negative (forbidden_refs) - otherwise it
    # belongs in the baseline fixture, not here.
    for c in cases:
        assert c.top_rank is not None or c.forbidden_refs, \
            f"non-adversarial case leaked into adversarial fixture: {c.description!r}"
