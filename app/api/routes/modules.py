from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from app.core.vector_store import get_db, get_module_versions
from app.core import graph as graph_db
from app.core.auth import require_reader
from app.models.schemas import ModuleResponse

router = APIRouter(prefix="/modules", tags=["modules"], dependencies=[require_reader])


@router.get("/", response_model=list[ModuleResponse])
async def list_modules(
    repo: str | None = None,
    tag: str | None = None,
    resource_type: str | None = None,
    version: str | None = None,
    module_path: str | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
):
    conditions = ["TRUE"]
    params: dict = {"limit": limit}

    if repo:
        conditions.append("repo = :repo")
        params["repo"] = repo
    if module_path:
        conditions.append("module_path = :module_path")
        params["module_path"] = module_path
    if tag:
        conditions.append(":tag = ANY(tags)")
        params["tag"] = tag
    if resource_type:
        conditions.append(":resource_type = ANY(resources)")
        params["resource_type"] = resource_type

    # Version filter: None → one per module (most recent), "*" → all rows, else specific
    if version is not None and version != "*":
        conditions.append("version = :version")
        params["version"] = version

    where = " AND ".join(conditions)

    if version is None:
        # Deduplicated: one row per (repo, module_path), most recently indexed
        result = await db.execute(
            text(f"""
                SELECT DISTINCT ON (repo, module_path)
                       id, repo, module_name, module_path, version, tags,
                       variables, outputs, resources, description, indexed_at, commit_sha
                FROM modules
                WHERE {where}
                ORDER BY repo, module_path, indexed_at DESC
                LIMIT :limit
            """),
            params,
        )
    else:
        result = await db.execute(
            text(f"""
                SELECT id, repo, module_name, module_path, version, tags,
                       variables, outputs, resources, description, indexed_at, commit_sha
                FROM modules
                WHERE {where}
                ORDER BY module_name ASC
                LIMIT :limit
            """),
            params,
        )
    return [ModuleResponse(**dict(r)) for r in result.mappings().all()]


@router.get("/versions/all")
async def list_all_versions(db: AsyncSession = Depends(get_db)):
    """List all versions in the database, sorted newest first (semver-aware).

    Branch refs (master/main/develop/HEAD) are sorted to the top as
    "rolling" refs. Pinned semver tags follow in descending order
    (v2.0.1 > v1.10.0 > v1.2.0). Non-semver tags sort last alphabetically.
    """
    import re
    result = await db.execute(
        text("SELECT DISTINCT version FROM modules")
    )
    versions = [r["version"] for r in result.mappings().all()]

    def _sort_key(v: str) -> tuple:
        # Branch refs first (group 0)
        if re.match(r'^(master|main|develop|HEAD)$', v):
            return (0, 0, 0, 0, v)
        # Pinned semver: extract first X.Y.Z, sort descending (group 1)
        m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', v)
        if m:
            return (1, -int(m.group(1)), -int(m.group(2)), -int(m.group(3) or 0), v)
        # Everything else (group 2)
        return (2, 0, 0, 0, v)

    versions.sort(key=_sort_key)
    return versions


@router.get("/repos/all")
async def list_all_repos(db: AsyncSession = Depends(get_db)):
    """List all repositories in the database."""
    result = await db.execute(
        text("SELECT DISTINCT repo FROM modules ORDER BY repo ASC")
    )
    return [r["repo"] for r in result.mappings().all()]


@router.get("/tags/all")
async def list_all_tags(db: AsyncSession = Depends(get_db)):
    """List all tags in the database with counts."""
    result = await db.execute(
        text("""
            SELECT tag, COUNT(*) AS module_count
            FROM modules, UNNEST(tags) AS tag
            GROUP BY tag
            ORDER BY module_count DESC
        """)
    )
    return [{"tag": r["tag"], "count": r["module_count"]}
            for r in result.mappings().all()]


@router.get("/{repo}/{module_path:path}/versions")
async def list_module_versions(
    repo: str,
    module_path: str,
    db: AsyncSession = Depends(get_db),
):
    """List all indexed versions for a specific module."""
    versions = await get_module_versions(db, repo, module_path)
    return {"repo": repo, "module_path": module_path, "versions": versions}


@router.get("/{repo}/{module_path:path}/dependencies")
async def get_dependencies(repo: str, module_path: str, version: str | None = None,
                           depth: int = 4):
    """Module dependency tree."""
    result = await graph_db.get_dependency_tree(
        module_path=module_path,
        depth=min(depth, 20),
        version=version,
        repo=repo,
    )
    return {"dependency_tree": result}


@router.get("/{repo}/{module_path:path}/dependents")
async def get_dependents(repo: str, module_path: str, version: str | None = None,
                         depth: int = 1):
    """Who depends on this module?"""
    dependents = await graph_db.find_dependents(module_path, repo, version=version,
                                                depth=depth)
    return {"dependents": dependents, "count": len(dependents)}
