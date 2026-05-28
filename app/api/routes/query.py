"""
Query layer — RAG search, code generation, dependency analysis.
"""
import time
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel
from typing import Optional

import yaml

from app.core.vector_store import get_db
from app.core.embeddings import embed_query
from app.core.auth import AuthenticatedUser, require_user, require_reader
from app.services.retriever import query as run_query, stream_query as run_stream_query
from app.core import graph as graph_db
from app.core import vector_store as vs
from app.models.schemas import QueryRequest, QueryResponse

router = APIRouter(prefix="/query", tags=["query"])


@router.post("/stream")
async def query_stream(request: QueryRequest, user: AuthenticatedUser = require_user, db: AsyncSession = Depends(get_db)):
    """Streaming RAG query — returns Server-Sent Events (SSE).

    Events:
    - {type: "sources", sources: [...]} — search results
    - {type: "token", token: "..."} — LLM answer chunk
    - {type: "error", message: str, latency_ms: N} — emitted when the LLM
      stream fails partway through (timeout, API error, etc.). Always
      followed by `done`.
    - {type: "done", latency_ms: N, ok: bool} — end of stream
    """
    return StreamingResponse(
        run_stream_query(request, db),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/", response_model=QueryResponse)
async def query(request: QueryRequest, user: AuthenticatedUser = require_user, db: AsyncSession = Depends(get_db)):
    """
    RAG query over the Terraform knowledge base.

    **query_type:**
    - `generate` — write new Terraform code based on existing modules
    - `optimize` — suggest improvements to existing code
    - `audit`    — security & tag compliance audit
    - `search`   — semantic search + Q&A
    """
    return await run_query(request, db)


class DependencyRequest(BaseModel):
    module_path: str
    repo: Optional[str] = None
    version: Optional[str] = None
    depth: int = 3


@router.post("/dependencies")
async def get_dependencies(req: DependencyRequest, user: AuthenticatedUser = require_reader):
    """Full dependency tree for a module."""
    # Treat "latest" as no version filter - there is no literal "latest"
    # version in the dependency table; it stores actual git tags.
    version = req.version if req.version and req.version != "latest" else None
    tree = await graph_db.get_dependency_tree(req.module_path, depth=req.depth,
                                              version=version, repo=req.repo)
    dependents = await graph_db.find_dependents(req.module_path, req.repo,
                                                version=version)
    providers = []
    # Find modules that provide outputs matching this module's variables
    # (useful for wiring new modules)
    return {
        "tree": tree,
        "dependents": dependents,
        "count_dependents": len(dependents),
    }


@router.get("/stats")
async def knowledge_base_stats(user: AuthenticatedUser = require_reader, db: AsyncSession = Depends(get_db)):
    """Overview stats of the indexed knowledge base."""
    result = await db.execute(text("""
        SELECT
            COUNT(DISTINCT (repo, module_path)) AS total_modules,
            COUNT(DISTINCT repo) AS total_repos,
            MAX(indexed_at)      AS last_indexed
        FROM modules
    """))
    row = dict(result.mappings().first())

    # UNNEST cannot be used inside COUNT(DISTINCT ...) — separate queries
    tag_count = await db.execute(text(
        "SELECT COUNT(DISTINCT tag) AS cnt FROM modules, UNNEST(tags) AS tag"
    ))
    row["unique_tags"] = tag_count.scalar() or 0

    res_count = await db.execute(text(
        "SELECT COUNT(DISTINCT rt) AS cnt FROM modules, UNNEST(resources) AS rt"
    ))
    row["unique_resource_types"] = res_count.scalar() or 0

    ver_count = await db.execute(text(
        "SELECT COUNT(DISTINCT version) AS cnt FROM modules"
    ))
    row["total_versions"] = ver_count.scalar() or 0

    # Top tags
    tags_result = await db.execute(text("""
        SELECT tag, COUNT(*) AS cnt
        FROM modules, UNNEST(tags) AS tag
        GROUP BY tag ORDER BY cnt DESC LIMIT 10
    """))
    top_tags = [{"tag": r["tag"], "count": r["cnt"]}
                for r in tags_result.mappings().all()]

    # Top resource types
    res_result = await db.execute(text("""
        SELECT rt, COUNT(*) AS cnt
        FROM modules, UNNEST(resources) AS rt
        GROUP BY rt ORDER BY cnt DESC LIMIT 10
    """))
    top_resources = [{"resource": r["rt"], "count": r["cnt"]}
                     for r in res_result.mappings().all()]

    # Knowledge snippet counts
    snippet_result = await db.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE kind LIKE 'convention.%%') AS total_conventions,
            COUNT(*) FILTER (WHERE kind = 'usage') AS total_usages
        FROM knowledge_snippets
    """))
    snippet_row = dict(snippet_result.mappings().first())

    return {**row, **snippet_row, "top_tags": top_tags, "top_resources": top_resources}


# -- Retrieval evaluation ------------------------------------------------------

EVAL_FIXTURE_PATH = Path(__file__).resolve().parents[3] / "scripts" / "eval_queries.yaml"


@router.post("/eval")
async def eval_retrieval(
    user: AuthenticatedUser = require_user,
    db: AsyncSession = Depends(get_db),
):
    """Run retrieval evaluation against the YAML fixture.

    Tests retrieval only (embedding + similarity search) - no LLM calls.
    Returns per-query hit/miss and aggregate recall.
    """
    if not EVAL_FIXTURE_PATH.exists():
        return {"error": f"Fixture not found: {EVAL_FIXTURE_PATH}"}

    with open(EVAL_FIXTURE_PATH) as f:
        cases = yaml.safe_load(f)

    if not isinstance(cases, list) or not cases:
        return {"error": "Fixture must be a non-empty YAML list"}

    results = []
    for entry in cases:
        query_text = entry.get("query", "")
        query_type = entry.get("query_type", "compose")
        expected_refs = entry.get("expected_refs", [])
        top_k = entry.get("top_k", 5)
        match_mode = entry.get("match", "any")

        t0 = time.monotonic()
        query_vec = embed_query(query_text, query_type=query_type)
        similar = await vs.similarity_search(
            db, query_embedding=query_vec, top_k=top_k,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        returned_refs = [
            f"{s['repo']}/{s['module_path']}" for s in similar
        ]
        matched = [r for r in expected_refs if r in returned_refs]
        missed = [r for r in expected_refs if r not in returned_refs]

        if match_mode == "all":
            hit = len(missed) == 0
        else:
            hit = len(matched) > 0

        results.append({
            "query": query_text,
            "query_type": query_type,
            "description": entry.get("description", ""),
            "hit": hit,
            "matched_refs": matched,
            "missed_refs": missed,
            "returned_refs": returned_refs,
            "latency_ms": latency_ms,
        })

    total = len(results)
    hits = sum(1 for r in results if r["hit"])
    all_expected = sum(len(r["missed_refs"]) + len(r["matched_refs"]) for r in results)
    all_matched = sum(len(r["matched_refs"]) for r in results)

    return {
        "summary": {
            "total_queries": total,
            "hits": hits,
            "misses": total - hits,
            "hit_rate_pct": round(hits / total * 100, 1) if total else 0,
            "ref_recall_pct": round(all_matched / all_expected * 100, 1) if all_expected else 0,
            "avg_latency_ms": sum(r["latency_ms"] for r in results) // total if total else 0,
        },
        "queries": results,
    }
