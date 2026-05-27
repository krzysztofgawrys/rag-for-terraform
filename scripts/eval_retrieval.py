#!/usr/bin/env python3
"""
Retrieval evaluation harness.

Runs a set of queries from a YAML fixture against a live RAG backend and
measures how often the expected modules appear in the returned sources.

Produces two things at once:
  - Technical: per-query hit/miss + aggregate recall (catches regressions)
  - Commercial: "X% retrieval accuracy on your modules" (POC ammunition)

Usage:
    # Against local dev
    python scripts/eval_retrieval.py

    # Against a deployed instance
    python scripts/eval_retrieval.py --url https://rag.example.com

    # Custom fixture
    python scripts/eval_retrieval.py --fixture scripts/eval_queries.yaml

    # JSON output (for CI)
    python scripts/eval_retrieval.py --json

    # With auth (API key or JWT)
    python scripts/eval_retrieval.py --api-key sk-...
    python scripts/eval_retrieval.py --token eyJ...

Exit codes:
    0 - all queries passed
    1 - at least one query missed
    2 - runtime error (network, bad fixture, etc.)
"""

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests
import yaml


DEFAULT_URL = "http://localhost:8000"
DEFAULT_FIXTURE = Path(__file__).parent / "eval_queries.yaml"


@dataclass
class QueryCase:
    query: str
    query_type: str = "compose"
    expected_refs: list[str] = field(default_factory=list)
    top_k: int = 5
    match: str = "any"  # "any" or "all"
    description: str = ""


@dataclass
class QueryResult:
    case: QueryCase
    returned_refs: list[str]
    hit: bool
    matched_refs: list[str]
    missed_refs: list[str]
    latency_ms: int
    error: str | None = None


def load_fixture(path: Path) -> list[QueryCase]:
    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, list):
        raise ValueError(f"Fixture must be a YAML list, got {type(raw).__name__}")

    cases = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"Entry {i} is not a dict")
        if "query" not in entry:
            raise ValueError(f"Entry {i} missing 'query' field")
        if "expected_refs" not in entry or not entry["expected_refs"]:
            raise ValueError(f"Entry {i} missing 'expected_refs'")

        cases.append(QueryCase(
            query=entry["query"],
            query_type=entry.get("query_type", "compose"),
            expected_refs=entry["expected_refs"],
            top_k=entry.get("top_k", 5),
            match=entry.get("match", "any"),
            description=entry.get("description", ""),
        ))
    return cases


def run_query(base_url: str, case: QueryCase,
              headers: dict) -> QueryResult:
    """Execute one query against POST /query/ and check results."""
    payload = {
        "query": case.query,
        "query_type": case.query_type,
        "top_k": case.top_k,
    }

    t0 = time.monotonic()
    try:
        resp = requests.post(
            f"{base_url}/query/",
            json=payload,
            headers=headers,
            timeout=120,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            return QueryResult(
                case=case,
                returned_refs=[],
                hit=False,
                matched_refs=[],
                missed_refs=case.expected_refs,
                latency_ms=latency_ms,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )

        data = resp.json()
    except Exception as exc:
        latency_ms = int((time.monotonic() - t0) * 1000)
        return QueryResult(
            case=case,
            returned_refs=[],
            hit=False,
            matched_refs=[],
            missed_refs=case.expected_refs,
            latency_ms=latency_ms,
            error=str(exc),
        )

    # Build returned refs as repo/module_path for comparison
    returned_refs = [
        f"{s['repo']}/{s['module_path']}" for s in data.get("sources", [])
    ]

    matched = [r for r in case.expected_refs if r in returned_refs]
    missed = [r for r in case.expected_refs if r not in returned_refs]

    if case.match == "all":
        hit = len(missed) == 0
    else:
        hit = len(matched) > 0

    return QueryResult(
        case=case,
        returned_refs=returned_refs,
        hit=hit,
        matched_refs=matched,
        missed_refs=missed,
        latency_ms=latency_ms,
    )


def print_report(results: list[QueryResult], as_json: bool = False):
    """Print human-readable or JSON report."""

    total = len(results)
    hits = sum(1 for r in results if r.hit)
    errors = sum(1 for r in results if r.error)
    hit_rate = (hits / total * 100) if total else 0
    avg_latency = (
        sum(r.latency_ms for r in results) // total if total else 0
    )

    # Per-ref recall: across all queries, how many expected refs were found?
    all_expected = sum(len(r.case.expected_refs) for r in results)
    all_matched = sum(len(r.matched_refs) for r in results)
    ref_recall = (all_matched / all_expected * 100) if all_expected else 0

    summary = {
        "total_queries": total,
        "hits": hits,
        "misses": total - hits,
        "errors": errors,
        "hit_rate_pct": round(hit_rate, 1),
        "ref_recall_pct": round(ref_recall, 1),
        "avg_latency_ms": avg_latency,
    }

    if as_json:
        detail = []
        for r in results:
            detail.append({
                "query": r.case.query,
                "query_type": r.case.query_type,
                "description": r.case.description,
                "hit": r.hit,
                "matched_refs": r.matched_refs,
                "missed_refs": r.missed_refs,
                "returned_refs": r.returned_refs,
                "latency_ms": r.latency_ms,
                "error": r.error,
            })
        print(json.dumps({"summary": summary, "queries": detail}, indent=2))
        return

    # Human report
    print()
    print("=" * 70)
    print("  RETRIEVAL EVALUATION REPORT")
    print("=" * 70)
    print()

    for i, r in enumerate(results, 1):
        status = "HIT " if r.hit else "MISS"
        if r.error:
            status = "ERR "
        label = r.case.description or r.case.query[:50]
        print(f"  {status}  [{r.latency_ms:>5}ms]  {label}")

        if r.missed_refs:
            for ref in r.missed_refs:
                print(f"          missing: {ref}")
        if r.error:
            print(f"          error: {r.error}")

    print()
    print("-" * 70)
    print(f"  Queries:       {hits}/{total} hit ({hit_rate:.0f}%)")
    print(f"  Module recall: {all_matched}/{all_expected} refs found ({ref_recall:.0f}%)")
    print(f"  Avg latency:   {avg_latency}ms")
    if errors:
        print(f"  Errors:        {errors}")
    print("-" * 70)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate retrieval quality against a YAML fixture."
    )
    parser.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"RAG backend URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--fixture", type=Path, default=DEFAULT_FIXTURE,
        help="Path to YAML fixture file",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_output",
        help="Output JSON instead of human-readable report",
    )
    parser.add_argument("--api-key", help="API key for Authorization header")
    parser.add_argument("--token", help="JWT Bearer token")
    args = parser.parse_args()

    # Auth headers
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if args.api_key:
        headers["X-API-Key"] = args.api_key
    elif args.token:
        headers["Authorization"] = f"Bearer {args.token}"

    # Load fixture
    try:
        cases = load_fixture(args.fixture)
    except Exception as exc:
        print(f"Failed to load fixture: {exc}", file=sys.stderr)
        sys.exit(2)

    if not cases:
        print("No test cases in fixture.", file=sys.stderr)
        sys.exit(2)

    if not args.json_output:
        print(f"Running {len(cases)} queries against {args.url} ...")
        print()

    # Run queries sequentially (deliberate - measures real latency)
    results = []
    for case in cases:
        result = run_query(args.url, case, headers)
        results.append(result)

        if not args.json_output:
            status = "." if result.hit else "X"
            print(status, end="", flush=True)

    if not args.json_output:
        print()

    print_report(results, as_json=args.json_output)

    # Exit code: 0 = all pass, 1 = at least one miss
    if all(r.hit for r in results):
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
