from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.vector_store import (
    get_db,
    list_module_refs_with_counts,
    list_consumer_repos,
    get_snippets_for_module,
)
from app.core.auth import require_user
from app.models.schemas import (
    ModuleRefSnippetSummary,
    SnippetModuleDetail,
    SnippetResponse,
)

router = APIRouter(prefix="/snippets", tags=["snippets"], dependencies=[require_user])


@router.get("/module-refs", response_model=list[ModuleRefSnippetSummary])
async def get_module_refs(
    kind: str | None = None,
    consumer_repo: str | None = None,
    q: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    rows = await list_module_refs_with_counts(
        db,
        kind_filter=kind,
        consumer_repo_filter=consumer_repo,
        module_ref_search=q,
    )
    return rows


@router.get("/consumer-repos", response_model=list[str])
async def get_consumer_repos(db: AsyncSession = Depends(get_db)):
    return await list_consumer_repos(db)


@router.get("/module-refs/{module_ref:path}", response_model=SnippetModuleDetail)
async def get_module_ref_detail(
    module_ref: str,
    db: AsyncSession = Depends(get_db),
):
    snippets = await get_snippets_for_module(db, module_ref, limit=100)

    conventions: dict[str, SnippetResponse] = {}
    usages: list[SnippetResponse] = []

    for s in snippets:
        sr = SnippetResponse(**s)
        if sr.kind.startswith("convention."):
            dim = sr.kind.removeprefix("convention.")
            conventions[dim] = sr
        elif sr.kind == "usage":
            usages.append(sr)

    return SnippetModuleDetail(
        module_ref=module_ref,
        conventions=conventions,
        usages=usages,
    )
