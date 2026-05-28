import re

import structlog
from sqlalchemy import text

from app.core.config import get_settings
from app.core.parser import ParsedModule
from app.core.vector_store import AsyncSessionLocal

log = structlog.get_logger()
settings = get_settings()

# Match git URL → extract repo name, module path inside repo, ref tag.
# Examples this should handle:
#   git::ssh://git@github.com/org/my-repo.git//ecs/cluster?ref=ecs_cluster-1.0.5
#   git@github.com:org/my-repo.git//modules/vpc?ref=v1.0.0
#   https://github.com/org/my-repo.git//foo
_GIT_URL_RE = re.compile(
    r'(?:git::)?'
    r'(?:ssh://(?:git@)?|https://|git@)'   # protocol
    r'[^/:]+[:/]'                         # host + : or /
    r'(?:[^/]+/)?'                        # optional org segment
    r'(?P<repo>[^/.?]+)'                  # repo name (no dots, no slashes)
    r'(?:\.git)?'                         # optional .git suffix
    r'//(?P<path>[^?#\s]+)'              # //<module-path> (greedy)
    r'(?:\?ref=(?P<ref>[^\s&"]+))?'       # optional ?ref=<tag>
)


def _parse_dep_source(dep_source: str, fallback_repo: str,
                      fallback_version: str) -> tuple[str, str, str]:
    """Parse a dependency source string into (repo, path, version).

    - Full git URL → extract repo, path, and ?ref= if present.
    - Plain path (e.g. `_infra_solutions/vpc` from `_resolve_source` on a
      relative source) → assume same repo as parent, version=fallback.
    - Other (registry refs like `cloudposse/vpc/aws`) → repo = first segment,
      path = remainder, version = fallback.
    """
    m = _GIT_URL_RE.search(dep_source)
    if m:
        repo = m.group("repo")
        path = (m.group("path") or "").strip("/")
        ref = m.group("ref") or fallback_version
        return repo, path, ref

    # Terraform registry format: namespace/name/provider
    # e.g. "cloudposse/security-group/aws" — 3rd segment is a cloud provider
    _REGISTRY_PROVIDERS = {"aws", "azurerm", "google", "gcp", "null", "kubernetes",
                           "helm", "tls", "random", "local", "archive", "http",
                           "external", "template", "vault", "datadog", "newrelic"}
    parts = dep_source.split("/")
    if (len(parts) == 3
            and all(p and not p.startswith(".") for p in parts)
            and parts[2] in _REGISTRY_PROVIDERS):
        return parts[0], f"{parts[1]}/{parts[2]}", fallback_version

    # Repo-local relative path (same repo as parent)
    return fallback_repo, dep_source, fallback_version


# -- Schema init ---------------------------------------------------------------

async def init_constraints():
    """No-op — schema is managed by SQL migrations."""
    log.info("graph.init_constraints: dependencies table managed by migrations")


# -- Lifecycle -----------------------------------------------------------------

async def close_driver():
    """No-op — no external driver to close (uses shared PostgreSQL pool)."""
    pass


# -- Write ----------------------------------------------------------------------

async def upsert_module(module: ParsedModule, db=None):
    """Store dependency edges for a module in PostgreSQL.

    When called from the indexer, pass the existing db session to avoid
    'another operation is in progress' errors on asyncpg.
    """
    async def _do(db):
        await db.execute(
            text("""
                DELETE FROM module_dependencies
                WHERE parent_repo = :repo
                  AND parent_path = :path
                  AND parent_version = :version
            """),
            dict(repo=module.repo, path=module.module_path,
                 version=module.version),
        )

        for dep_source in module.dependencies:
            dep_repo, dep_path, dep_version = _parse_dep_source(
                dep_source,
                fallback_repo=module.repo,
                fallback_version=module.version,
            )
            await db.execute(
                text("""
                    INSERT INTO module_dependencies
                        (parent_repo, parent_path, parent_version,
                         dep_repo, dep_path, dep_version, dep_name)
                    VALUES (:parent_repo, :parent_path, :parent_version,
                            :dep_repo, :dep_path, :dep_version, :dep_name)
                    ON CONFLICT DO NOTHING
                """),
                dict(
                    parent_repo=module.repo,
                    parent_path=module.module_path,
                    parent_version=module.version,
                    dep_repo=dep_repo,
                    dep_path=dep_path,
                    dep_version=dep_version,
                    dep_name=(dep_repo if dep_path in ("", ".") else dep_path.strip("/").split("/")[-1]),
                ),
            )
        await db.commit()

    if db is not None:
        await _do(db)
    else:
        async with AsyncSessionLocal() as db:
            await _do(db)


async def delete_module(repo: str, module_path: str, version: str, db=None):
    """Delete all dependency edges involving this module (as parent or child)."""
    async def _do(db):
        await db.execute(
            text("""
                DELETE FROM module_dependencies
                WHERE (parent_repo = :repo AND parent_path = :path AND parent_version = :version)
                   OR (dep_repo = :repo AND dep_path = :path AND dep_version = :version)
            """),
            dict(repo=repo, path=module_path, version=version),
        )
        await db.commit()

    if db is not None:
        await _do(db)
    else:
        async with AsyncSessionLocal() as db:
            await _do(db)


# -- Read ----------------------------------------------------------------------

async def find_dependents(module_path: str, repo: str | None = None,
                         version: str | None = None,
                         depth: int = 1) -> list[dict]:
    """Who depends on this module? depth=1 for direct only, >1 for recursive."""
    version_filter = "AND md.dep_version = :version" if version else ""
    repo_filter = "AND md.dep_repo = :repo" if repo else ""

    if depth <= 1:
        query = f"""
            SELECT DISTINCT
                COALESCE(m.module_name, CASE WHEN md.parent_path = '.' THEN md.parent_repo ELSE split_part(md.parent_path, '/', -1) END) AS name,
                md.parent_repo AS repo,
                md.parent_path AS path,
                md.parent_version AS version
            FROM module_dependencies md
            LEFT JOIN modules m
              ON m.repo = md.parent_repo
             AND m.module_path = md.parent_path
             AND m.version = md.parent_version
            WHERE md.dep_path = :path
              {repo_filter}
              {version_filter}
        """
    else:
        query = f"""
            WITH RECURSIVE reverse_deps AS (
                SELECT md.parent_repo, md.parent_path, md.parent_version, 1 AS lvl
                FROM module_dependencies md
                WHERE md.dep_path = :path
                  {repo_filter}
                  {version_filter}

                UNION

                SELECT md.parent_repo, md.parent_path, md.parent_version, rd.lvl + 1
                FROM module_dependencies md
                JOIN reverse_deps rd
                  ON md.dep_repo = rd.parent_repo
                 AND md.dep_path = rd.parent_path
                 AND md.dep_version = rd.parent_version
                WHERE rd.lvl < :depth
            )
            SELECT DISTINCT
                COALESCE(m.module_name, CASE WHEN rd.parent_path = '.' THEN rd.parent_repo ELSE split_part(rd.parent_path, '/', -1) END) AS name,
                rd.parent_repo AS repo,
                rd.parent_path AS path,
                rd.parent_version AS version
            FROM reverse_deps rd
            LEFT JOIN modules m
              ON m.repo = rd.parent_repo
             AND m.module_path = rd.parent_path
             AND m.version = rd.parent_version
        """

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(query),
            dict(path=module_path, repo=repo, version=version, depth=min(depth, 20)),
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def find_modules_with_tag(tag: str) -> list[dict]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("""
                SELECT module_name AS name, repo, module_path AS path
                FROM modules
                WHERE :tag = ANY(tags)
            """),
            dict(tag=tag),
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def find_providers_of_output(output_name: str) -> list[dict]:
    """Which modules produce an output with this name?"""
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("""
                SELECT module_name AS name, repo, module_path AS path
                FROM modules
                WHERE outputs ? :out
            """),
            dict(out=output_name),
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def get_direct_dependencies(
    repo: str, module_path: str, version: str | None = None,
) -> list[dict]:
    """Return immediate (1-hop) dependencies of a module."""
    version_filter = "AND parent_version = :version" if version else ""

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(f"""
                SELECT DISTINCT
                    dep_repo AS repo,
                    dep_path AS path,
                    dep_version AS version,
                    CASE WHEN dep_path = '.' THEN dep_repo ELSE dep_name END AS name
                FROM module_dependencies
                WHERE parent_repo = :repo
                  AND parent_path = :path
                  {version_filter}
                LIMIT 20
            """),
            dict(repo=repo, path=module_path, version=version),
        )
        return [dict(r._mapping) for r in result.fetchall()]


async def get_dependency_tree(module_path: str, depth: int = 3,
                             version: str | None = None,
                             repo: str | None = None) -> list[dict]:
    """Return dependency tree (up to N levels deep)."""
    version_filter = "AND md.parent_version = :version" if version else ""
    repo_filter = "AND md.parent_repo = :repo" if repo else ""

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text(f"""
                WITH RECURSIVE dep_tree AS (
                    -- Anchor: direct deps of the target module
                    SELECT
                        md.dep_path,
                        md.dep_repo,
                        md.dep_version,
                        md.dep_name,
                        ARRAY[
                            md.parent_repo || '//' || md.parent_path,
                            md.dep_repo || '//' || md.dep_path
                        ] AS chain,
                        ARRAY[
                            COALESCE(m.module_name, CASE WHEN md.parent_path = '.' THEN md.parent_repo ELSE split_part(md.parent_path, '/', -1) END),
                            CASE WHEN md.dep_path = '.' THEN md.dep_repo ELSE md.dep_name END
                        ] AS chain_names,
                        ARRAY[
                            md.parent_version,
                            md.dep_version
                        ] AS chain_versions,
                        ARRAY[md.parent_repo || '/' || md.parent_path,
                              md.dep_repo || '/' || md.dep_path] AS visited,
                        1 AS depth
                    FROM module_dependencies md
                    LEFT JOIN modules m
                      ON m.repo = md.parent_repo
                     AND m.module_path = md.parent_path
                     AND m.version = md.parent_version
                    WHERE md.parent_path = :path
                      {repo_filter}
                      {version_filter}

                    UNION ALL

                    -- Recursive: follow dependencies deeper
                    SELECT
                        md.dep_path,
                        md.dep_repo,
                        md.dep_version,
                        md.dep_name,
                        dt.chain || (md.dep_repo || '//' || md.dep_path),
                        dt.chain_names || CASE WHEN md.dep_path = '.' THEN md.dep_repo ELSE md.dep_name END,
                        dt.chain_versions || md.dep_version,
                        dt.visited || (md.dep_repo || '/' || md.dep_path),
                        dt.depth + 1
                    FROM module_dependencies md
                    JOIN dep_tree dt
                      ON md.parent_path = dt.dep_path
                     AND md.parent_repo = dt.dep_repo
                     AND md.parent_version = dt.dep_version
                    WHERE dt.depth < :max_depth
                      AND NOT (md.dep_repo || '/' || md.dep_path) = ANY(dt.visited)
                )
                SELECT DISTINCT chain, chain_names, chain_versions, dep_name, dep_repo, dep_path
                FROM dep_tree
            """),
            dict(path=module_path, repo=repo, version=version, max_depth=depth),
        )
        return [dict(r._mapping) for r in result.fetchall()]
