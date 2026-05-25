"""
Query layer — RAG search, code generation, dependency analysis.
"""
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel
from typing import Optional

from app.core.vector_store import get_db
from app.core.auth import AuthenticatedUser, require_user
from app.services.retriever import query as run_query, stream_query as run_stream_query
from app.core import graph as graph_db
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
    version: str = "latest"
    depth: int = 3


@router.post("/dependencies")
async def get_dependencies(req: DependencyRequest, user: AuthenticatedUser = require_user):
    """Full dependency tree for a module."""
    tree = await graph_db.get_dependency_tree(req.module_path, depth=req.depth,
                                              version=req.version)
    dependents = await graph_db.find_dependents(req.module_path, req.repo,
                                                version=req.version)
    providers = []
    # Find modules that provide outputs matching this module's variables
    # (useful for wiring new modules)
    return {
        "tree": tree,
        "dependents": dependents,
        "count_dependents": len(dependents),
    }


@router.get("/stats")
async def knowledge_base_stats(user: AuthenticatedUser = require_user, db: AsyncSession = Depends(get_db)):
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
