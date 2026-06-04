"""Tests for the description-eval reply parser (_parse_eval_response).

The gate's purpose is to catch low-quality descriptions. The historical bug:
the eval LLM's JSON was truncated by a tight token cap mid-`issues`, strict
JSON parsing failed, and the code defaulted to a PASSING score of 3 - so a
genuine score-2 with a verbose explanation slipped through. The parser now
recovers the score (which comes first in the schema) from a truncated reply.
"""
from app.services.indexer import _parse_eval_response


def test_clean_json():
    assert _parse_eval_response('{"score": 4, "issues": ""}') == (4, "")


def test_fenced_json():
    assert _parse_eval_response('```json\n{"score": 2, "issues": "bad port"}\n```') == (2, "bad port")


def test_prose_wrapped_json():
    score, _ = _parse_eval_response('Here is my assessment: {"score": 5, "issues": ""}. Done.')
    assert score == 5


def test_truncated_low_score_is_read_not_passed():
    # THE furtka: token cap cut the reply mid-issues, no closing brace. The
    # score must still read 2 (-> fallback), NOT silently default to a passing 3.
    raw = ('{"score": 2, "issues": "the description claims TCP 7199 but the rule '
           'default web-jmx-tcp maps to 9010 and the port is not grounded in the')
    score, _ = _parse_eval_response(raw)
    assert score == 2


def test_partial_json_score_only():
    score, issues = _parse_eval_response('{"score": 3, "iss')
    assert score == 3
    assert issues == "eval_partial_parse"


def test_score_clamped_to_range():
    assert _parse_eval_response('{"score": 7}')[0] == 5
    assert _parse_eval_response('{"score": 0}')[0] == 1


def test_score_with_equals_and_no_quotes():
    assert _parse_eval_response('score = 4, issues = "ok"')[0] == 4


def test_garbage_returns_none():
    score, reason = _parse_eval_response('I am unable to evaluate this module.')
    assert score is None
    assert reason == "eval_unparseable"


def test_empty_or_none_returns_none():
    assert _parse_eval_response('')[0] is None
    assert _parse_eval_response('   ')[0] is None
    assert _parse_eval_response(None) == (None, "eval_empty")
