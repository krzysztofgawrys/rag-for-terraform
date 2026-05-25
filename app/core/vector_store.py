import structlog
from uuid import UUID
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from pgvector.sqlalchemy import Vector
from app.core.config import get_settings
from app.core.parser import ParsedModule

log = structlog.get_logger()
settings = get_settings()

engine = create_async_engine(settings.database_url, echo=settings.debug, pool_size=10)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def make_session_factory() -> tuple["AsyncEngine", sessionmaker]:
    """Create a new engine + session factory bound to the current event loop.

    Use this in Celery tasks (which run asyncio.run() creating a fresh loop)
    to avoid 'Future attached to a different loop' errors.

    Returns (engine, sessionmaker) so the caller can dispose the engine.
    """
    e = create_async_engine(settings.database_url, echo=settings.debug, pool_size=5)
    return e, sessionmaker(e, class_=AsyncSession, expire_on_commit=False)


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# -- Write ----------------------------------------------------------------------

async def upsert_module(
    db: AsyncSession,
    module: ParsedModule,
    embedding: list[float],
    description: str,
    commit_sha: str | None = None,
    job_id: str | None = None,
    code_hash: str | None = None,
) -> UUID:
    """Insert or update a module. Returns the module UUID."""
    result = await db.execute(
        text("""
            INSERT INTO modules
                (repo, module_name, module_path, version, tags, variables, outputs,
                 resources, description, raw_code, embedding, commit_sha, job_id, code_hash)
            VALUES
                (:repo, :module_name, :module_path, :version, :tags, CAST(:variables AS jsonb),
                 CAST(:outputs AS jsonb), :resources, :description, :raw_code,
                 :embedding, :commit_sha,
                 (SELECT id FROM index_jobs WHERE id = CAST(:job_id AS uuid)),
                 :code_hash)
            ON CONFLICT (repo, module_path, version)
            DO UPDATE SET
                tags        = EXCLUDED.tags,
                variables   = EXCLUDED.variables,
                outputs     = EXCLUDED.outputs,
                resources   = EXCLUDED.resources,
                description = EXCLUDED.description,
                raw_code    = EXCLUDED.raw_code,
                embedding   = EXCLUDED.embedding,
                commit_sha  = EXCLUDED.commit_sha,
                job_id      = EXCLUDED.job_id,
                code_hash   = EXCLUDED.code_hash,
                indexed_at  = now()
            RETURNING id
        """),
        {
            "repo": module.repo,
            "module_name": module.module_name,
            "module_path": module.module_path,
            "version": module.version,
            "tags": module.tags,
            "variables": __import__("json").dumps(module.variables),
            "outputs": __import__("json").dumps(module.outputs),
            "resources": module.resources,
            "description": description,
            "raw_code": module.raw_code[:50_000],  # cap at 50k chars
            "embedding": str(embedding),
            "commit_sha": commit_sha,
            "job_id": job_id,
            "code_hash": code_hash,
        },
    )
    await db.commit()
    row = result.fetchone()
    return row[0]


async def find_by_code_hash(
    db: AsyncSession, repo: str, module_path: str, code_hash: str,
) -> dict | None:
    """Find an existing module with the same code hash (any version).
    Returns description and embedding as text for direct reuse."""
    result = await db.execute(
        text("""
            SELECT description, embedding::text AS embedding_str
            FROM modules
            WHERE repo = :repo AND module_path = :path AND code_hash = :hash
              AND description IS NOT NULL AND description != ''
            LIMIT 1
        """),
        {"repo": repo, "path": module_path, "hash": code_hash},
    )
    row = result.mappings().first()
    return dict(row) if row else None


# -- Search ---------------------------------------------------------------------

async def similarity_search(
    db: AsyncSession,
    query_embedding: list[float],
    top_k: int = 5,
    repo_filter: list[str] | str | None = None,
    tag_filter: list[str] | None = None,
    version_filter: list[str] | str | None = None,
) -> list[dict]:
    """Cosine similarity search with optional repo/tag/version filters.

    repo_filter: None → all repos, str → single, list → any of.
    tag_filter: None → all tags, list → modules matching ANY of the tags.
    version_filter: None → latest only, "*" → all versions, str → specific,
                    list → any of the versions.
    """

    conditions = ["TRUE"]
    params: dict = {"embedding": str(query_embedding), "top_k": top_k}

    if repo_filter:
        if isinstance(repo_filter, list):
            conditions.append("repo = ANY(:repo_filter)")
            params["repo_filter"] = repo_filter
        else:
            conditions.append("repo = :repo_filter")
            params["repo_filter"] = repo_filter

    if tag_filter:
        conditions.append("tags && :tag_filter")
        params["tag_filter"] = tag_filter

    # Normalise version_filter: "*" means all versions, None means latest
    _vf_all = False
    if version_filter is not None:
        if isinstance(version_filter, str):
            if version_filter == "*":
                _vf_all = True
            else:
                conditions.append("version = :version_filter")
                params["version_filter"] = version_filter
        elif isinstance(version_filter, list):
            if "*" in version_filter:
                _vf_all = True
            else:
                conditions.append("version = ANY(:version_filter)")
                params["version_filter"] = version_filter

    where = " AND ".join(conditions)

    if version_filter is None and not _vf_all:
        # Default: one row per module — latest semver version.
        # Step 1: pick the latest semver version per (repo, module_path).
        # Step 2: rank those by cosine similarity.
        # This ensures the LLM always sees variables/outputs from the
        # newest release, not from whichever version happened to have
        # the closest embedding (often an old tag like v1.0.0).
        result = await db.execute(
            text(f"""
                WITH latest AS (
                    SELECT DISTINCT ON (repo, module_path) *
                    FROM modules
                    WHERE {where}
                    ORDER BY repo, module_path,
                        CASE WHEN version ~ '^(master|main|develop|HEAD)$'
                             THEN 1 ELSE 0 END,
                        (regexp_match(version, '(\\d+)\\.(\\d+)(?:\\.(\\d+))?'))[1]::int DESC NULLS LAST,
                        (regexp_match(version, '(\\d+)\\.(\\d+)(?:\\.(\\d+))?'))[2]::int DESC NULLS LAST,
                        COALESCE((regexp_match(version, '(\\d+)\\.(\\d+)(?:\\.(\\d+))?'))[3]::int, 0) DESC
                )
                SELECT id, repo, module_name, module_path, version, tags,
                       variables, outputs, resources, description,
                       1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                FROM latest
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :top_k
            """),
            params,
        )
        return [dict(r) for r in result.mappings().all()]
    else:
        result = await db.execute(
            text(f"""
                SELECT
                    id, repo, module_name, module_path, version, tags,
                    variables, outputs, resources, description,
                    1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
                FROM modules
                WHERE {where}
                ORDER BY embedding <=> CAST(:embedding AS vector)
                LIMIT :top_k
            """),
            params,
        )
        return [dict(r) for r in result.mappings().all()]


async def get_modules_by_tag(db: AsyncSession, tag: str) -> list[dict]:
    result = await db.execute(
        text("SELECT id, repo, module_name, module_path, tags FROM modules WHERE :tag = ANY(tags)"),
        {"tag": tag},
    )
    return [dict(r) for r in result.mappings().all()]


async def get_module_by_path(db: AsyncSession, repo: str, module_path: str,
                            version: str | None = None) -> dict | None:
    """Get a module by (repo, module_path[, version]).

    When `version` is None, returns the semantically latest version
    (highest semver, branch refs excluded). Falls back to most recently
    indexed row if no semver tags exist for this module.
    """
    if version:
        result = await db.execute(
            text("SELECT * FROM modules WHERE repo = :repo AND module_path = :path AND version = :version"),
            {"repo": repo, "path": module_path, "version": version},
        )
        row = result.mappings().first()
        return dict(row) if row else None

    # version=None → use semver-aware sort, then fall back to indexed_at
    versions = await get_module_versions(db, repo, module_path)
    if not versions:
        return None
    # Prefer the first pinned (non-branch) version, else fall back to head row
    pinned = next(
        (v for v in versions
         if v["version"] not in ("master", "main", "develop", "HEAD")),
        versions[0],
    )
    result = await db.execute(
        text("SELECT * FROM modules WHERE repo = :repo AND module_path = :path AND version = :version"),
        {"repo": repo, "path": module_path, "version": pinned["version"]},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def get_module_versions(db: AsyncSession, repo: str, module_path: str) -> list[dict]:
    """Return all indexed versions for a given module, sorted newest first."""
    import re
    result = await db.execute(
        text("""
            SELECT version, commit_sha, indexed_at
            FROM modules
            WHERE repo = :repo AND module_path = :path
        """),
        {"repo": repo, "path": module_path},
    )
    rows = [dict(r) for r in result.mappings().all()]

    def _version_sort_key(row: dict) -> tuple:
        v = row["version"]
        # Branches (master, main, develop) → sort first (highest priority)
        if re.match(r'^(master|main|develop)$', v):
            return (0, 0, 0, 0, v)
        # Extract version numbers from anywhere in the string
        m = re.search(r'(\d+)\.(\d+)(?:\.(\d+))?', v)
        if m:
            return (1, -int(m.group(1)), -int(m.group(2)), -int(m.group(3) or 0), v)
        # Everything else → sort last alphabetically
        return (2, 0, 0, 0, v)

    rows.sort(key=_version_sort_key)
    return rows


# -- Index Job -----------------------------------------------------------------

async def create_index_job(db: AsyncSession, repo: str, branch: str,
                           commit_sha: str | None, triggered_by: str,
                           repo_url: str | None = None) -> UUID:
    result = await db.execute(
        text("""
            INSERT INTO index_jobs (repo, repo_url, branch, commit_sha, triggered_by, status)
            VALUES (:repo, :repo_url, :branch, :commit_sha, :triggered_by, 'pending')
            RETURNING id
        """),
        {"repo": repo, "repo_url": repo_url, "branch": branch,
         "commit_sha": commit_sha, "triggered_by": triggered_by},
    )
    await db.commit()
    return result.scalar()


_INDEX_JOB_COLUMNS = frozenset({
    "status", "started_at", "finished_at", "stats", "error",
})


async def delete_index_job(db: AsyncSession, job_id: UUID) -> dict:
    """Delete an index job and all modules it created. Returns counts."""
    # Get modules that will be deleted (for dependency cleanup)
    result = await db.execute(
        text("SELECT repo, module_path, version FROM modules WHERE job_id = :job_id"),
        {"job_id": job_id},
    )
    modules_to_delete = [dict(r) for r in result.mappings().all()]

    # Delete modules
    del_result = await db.execute(
        text("DELETE FROM modules WHERE job_id = :job_id"),
        {"job_id": job_id},
    )
    modules_deleted = del_result.rowcount

    # Delete the job itself
    await db.execute(
        text("DELETE FROM index_jobs WHERE id = :job_id"),
        {"job_id": job_id},
    )
    await db.commit()

    return {"modules_deleted": modules_deleted, "modules": modules_to_delete}


async def update_index_job(db: AsyncSession, job_id: UUID, **kwargs):
    invalid = set(kwargs) - _INDEX_JOB_COLUMNS
    if invalid:
        raise ValueError(f"Invalid index_job columns: {invalid}")
    set_parts = ", ".join(f"{k} = :{k}" for k in kwargs)
    await db.execute(
        text(f"UPDATE index_jobs SET {set_parts} WHERE id = :job_id"),
        {"job_id": job_id, **kwargs},
    )
    await db.commit()


# -- Knowledge Snippets -------------------------------------------------------

async def get_existing_convention_quality(
    db: AsyncSession, module_ref: str, kind: str,
) -> tuple[int | None, int | None]:
    """Get (eval_score, evidence_count) of existing convention snippet.

    Returns (None, None) if not found or stale.
    """
    result = await db.execute(
        text("""
            SELECT eval_score, evidence_count FROM knowledge_snippets
            WHERE module_ref = :module_ref AND kind = :kind
              AND stale IS NOT TRUE
        """),
        {"module_ref": module_ref, "kind": kind},
    )
    row = result.mappings().first()
    if row is None:
        return None, None
    return row["eval_score"], row["evidence_count"]


async def upsert_snippet(
    db: AsyncSession,
    kind: str,
    module_ref: str,
    summary: str,
    embedding: list[float],
    evidence_count: int = 1,
    scope: str | None = None,
    source_locator: str | None = None,
    related_refs: list[str] | None = None,
    consumer_repo: str | None = None,
    eval_score: int | None = None,
) -> UUID:
    """Insert or update a knowledge snippet.

    For 'usage' kind: inserts a new row (one per observation).
    For 'convention.*' kind: upserts by (module_ref, kind) — one per dimension.
    """
    if kind.startswith("convention.") or kind == "stack_pattern":
        # Upsert: one convention per module_ref per dimension
        result = await db.execute(
            text("""
                INSERT INTO knowledge_snippets
                    (kind, module_ref, scope, summary, evidence_count,
                     source_locator, related_refs, consumer_repo, embedding,
                     eval_score, updated_at)
                VALUES
                    (:kind, :module_ref, :scope, :summary, :evidence_count,
                     :source_locator, :related_refs, :consumer_repo,
                     CAST(:embedding AS vector), :eval_score, now())
                ON CONFLICT (module_ref, kind)
                    WHERE kind LIKE 'convention.%' OR kind = 'stack_pattern'
                DO UPDATE SET
                    summary = EXCLUDED.summary,
                    evidence_count = EXCLUDED.evidence_count,
                    embedding = EXCLUDED.embedding,
                    related_refs = EXCLUDED.related_refs,
                    eval_score = EXCLUDED.eval_score,
                    stale = FALSE,
                    updated_at = now()
                RETURNING id
            """),
            {
                "kind": kind, "module_ref": module_ref, "scope": scope,
                "summary": summary, "evidence_count": evidence_count,
                "source_locator": source_locator,
                "related_refs": related_refs,
                "consumer_repo": consumer_repo,
                "embedding": str(embedding),
                "eval_score": eval_score,
            },
        )
    else:
        # Insert: each usage is a separate row
        result = await db.execute(
            text("""
                INSERT INTO knowledge_snippets
                    (kind, module_ref, scope, summary, evidence_count,
                     source_locator, related_refs, consumer_repo, embedding)
                VALUES
                    (:kind, :module_ref, :scope, :summary, :evidence_count,
                     :source_locator, :related_refs, :consumer_repo,
                     CAST(:embedding AS vector))
                RETURNING id
            """),
            {
                "kind": kind, "module_ref": module_ref, "scope": scope,
                "summary": summary, "evidence_count": evidence_count,
                "source_locator": source_locator,
                "related_refs": related_refs,
                "consumer_repo": consumer_repo,
                "embedding": str(embedding),
            },
        )
    await db.commit()
    return result.scalar()


async def delete_snippets_by_consumer(db: AsyncSession, consumer_repo: str) -> int:
    """Delete usage + compose_pattern snippets for a given consumer repo
    (idempotent re-index). Conventions are NOT deleted — they're rebuilt
    by the distiller from the new usage data."""
    result = await db.execute(
        text("DELETE FROM knowledge_snippets WHERE consumer_repo = :repo "
             "AND kind IN ('usage', 'compose_pattern')"),
        {"repo": consumer_repo},
    )
    await db.commit()
    return result.rowcount


async def mark_snippet_stale(
    db: AsyncSession, module_ref: str, kind: str,
) -> bool:
    """Mark an existing convention snippet as stale (quality gate failed).

    Returns True if a row was updated, False if no matching snippet exists.
    """
    result = await db.execute(
        text("""
            UPDATE knowledge_snippets
            SET stale = TRUE, updated_at = now()
            WHERE module_ref = :module_ref AND kind = :kind
              AND (kind LIKE 'convention.%%' OR kind = 'stack_pattern')
        """),
        {"module_ref": module_ref, "kind": kind},
    )
    await db.commit()
    return result.rowcount > 0


async def mark_module_conventions_stale(
    db: AsyncSession, module_ref: str,
) -> int:
    """Mark ALL convention snippets for a module as stale (no usage left).

    Returns count of rows updated.
    """
    result = await db.execute(
        text("""
            UPDATE knowledge_snippets
            SET stale = TRUE, updated_at = now()
            WHERE module_ref = :module_ref
              AND kind LIKE 'convention.%%'
              AND stale IS NOT TRUE
        """),
        {"module_ref": module_ref},
    )
    await db.commit()
    return result.rowcount


async def get_usage_summaries(db: AsyncSession, module_ref: str) -> list[str]:
    """Get all usage summary texts for a module_ref (for distillation input)."""
    result = await db.execute(
        text("""
            SELECT summary FROM knowledge_snippets
            WHERE module_ref = :module_ref AND kind = 'usage'
            ORDER BY created_at
        """),
        {"module_ref": module_ref},
    )
    return [row["summary"] for row in result.mappings().all()]


async def get_snippets_for_module(
    db: AsyncSession,
    module_ref: str,
    kinds: list[str] | None = None,
    limit: int = 30,
) -> list[dict]:
    """Get knowledge snippets for a module, optionally filtered by kind."""
    if kinds:
        result = await db.execute(
            text("""
                SELECT id, kind, summary, evidence_count, eval_score,
                       source_locator, related_refs, scope, consumer_repo,
                       updated_at, COALESCE(stale, FALSE) AS stale
                FROM knowledge_snippets
                WHERE module_ref = :module_ref AND kind = ANY(:kinds)
                ORDER BY stale ASC, evidence_count DESC, updated_at DESC
                LIMIT :limit
            """),
            {"module_ref": module_ref, "kinds": kinds, "limit": limit},
        )
    else:
        result = await db.execute(
            text("""
                SELECT id, kind, summary, evidence_count, eval_score,
                       source_locator, related_refs, scope, consumer_repo,
                       updated_at, COALESCE(stale, FALSE) AS stale
                FROM knowledge_snippets
                WHERE module_ref = :module_ref
                ORDER BY stale ASC, evidence_count DESC, updated_at DESC
                LIMIT :limit
            """),
            {"module_ref": module_ref, "limit": limit},
        )
    return [dict(r) for r in result.mappings().all()]


async def snippet_similarity_search(
    db: AsyncSession,
    query_embedding: list[float],
    top_k: int = 10,
    kind_filter: list[str] | None = None,
    module_ref_filter: str | None = None,
) -> list[dict]:
    """Cosine similarity search on knowledge_snippets."""
    conditions = ["TRUE"]
    params: dict = {"embedding": str(query_embedding), "top_k": top_k}

    if kind_filter:
        conditions.append("kind = ANY(:kind_filter)")
        params["kind_filter"] = kind_filter

    if module_ref_filter:
        conditions.append("module_ref = :module_ref")
        params["module_ref"] = module_ref_filter

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT id, kind, module_ref, summary, evidence_count,
                   source_locator, related_refs, scope,
                   1 - (embedding <=> CAST(:embedding AS vector)) AS similarity
            FROM knowledge_snippets
            WHERE {where}
            ORDER BY embedding <=> CAST(:embedding AS vector)
            LIMIT :top_k
        """),
        params,
    )
    return [dict(r) for r in result.mappings().all()]


async def list_module_refs_with_counts(
    db: AsyncSession,
    kind_filter: str | None = None,
    consumer_repo_filter: str | None = None,
    module_ref_search: str | None = None,
) -> list[dict]:
    """List module_refs with usage/convention counts for the Knowledge browser.

    Excludes aggregate-pattern snippets whose `module_ref` is a synthetic
    identifier (`compose:<repo>:<path>` for compose_patterns,
    `stack:<sig>` for stack_patterns). Those rows are technical pipeline
    artefacts — clicking them would yield an empty detail view because
    they carry neither `usage` nor `convention.*` snippets.
    """
    # Always exclude synthetic module_refs used by compose_pattern /
    # stack_pattern aggregates — they're not browseable modules.
    conditions = [
        "module_ref NOT LIKE 'compose:%'",
        "module_ref NOT LIKE 'stack:%'",
    ]
    params: dict = {}

    if kind_filter:
        conditions.append("kind LIKE :kind_filter")
        params["kind_filter"] = f"{kind_filter}%"

    if consumer_repo_filter:
        conditions.append("consumer_repo = :consumer_repo")
        params["consumer_repo"] = consumer_repo_filter

    if module_ref_search:
        conditions.append("module_ref ILIKE :search")
        params["search"] = f"%{module_ref_search}%"

    where = " AND ".join(conditions)

    result = await db.execute(
        text(f"""
            SELECT
                module_ref,
                COUNT(*) FILTER (WHERE kind = 'usage') AS usage_count,
                COUNT(*) FILTER (WHERE kind LIKE 'convention.%%') AS convention_count,
                ARRAY_AGG(DISTINCT kind) AS kinds
            FROM knowledge_snippets
            WHERE {where}
            GROUP BY module_ref
            ORDER BY module_ref
        """),
        params,
    )
    return [dict(r) for r in result.mappings().all()]


async def list_consumer_repos(db: AsyncSession) -> list[str]:
    """List distinct consumer repos that have snippets."""
    result = await db.execute(
        text("SELECT DISTINCT consumer_repo FROM knowledge_snippets WHERE consumer_repo IS NOT NULL ORDER BY consumer_repo"),
    )
    return [row["consumer_repo"] for row in result.mappings().all()]


async def get_affected_module_refs(db: AsyncSession, consumer_repo: str) -> list[str]:
    """Get distinct module_refs that have usage snippets from a consumer repo."""
    result = await db.execute(
        text("""
            SELECT DISTINCT module_ref FROM knowledge_snippets
            WHERE consumer_repo = :repo AND kind = 'usage'
        """),
        {"repo": consumer_repo},
    )
    return [row["module_ref"] for row in result.mappings().all()]


# -- Consumer Index Jobs ------------------------------------------------------

async def create_consumer_index_job(db: AsyncSession, repo: str, branch: str,
                                     commit_sha: str | None, triggered_by: str,
                                     repo_url: str | None = None) -> UUID:
    result = await db.execute(
        text("""
            INSERT INTO consumer_index_jobs (repo, repo_url, branch, commit_sha, triggered_by, status)
            VALUES (:repo, :repo_url, :branch, :commit_sha, :triggered_by, 'pending')
            RETURNING id
        """),
        {"repo": repo, "repo_url": repo_url, "branch": branch,
         "commit_sha": commit_sha, "triggered_by": triggered_by},
    )
    await db.commit()
    return result.scalar()


_CONSUMER_JOB_COLUMNS = frozenset({
    "status", "started_at", "finished_at", "stats", "error",
})


async def update_consumer_index_job(db: AsyncSession, job_id: UUID, **kwargs):
    invalid = set(kwargs) - _CONSUMER_JOB_COLUMNS
    if invalid:
        raise ValueError(f"Invalid consumer_index_job columns: {invalid}")
    set_parts = ", ".join(f"{k} = :{k}" for k in kwargs)
    await db.execute(
        text(f"UPDATE consumer_index_jobs SET {set_parts} WHERE id = :job_id"),
        {"job_id": job_id, **kwargs},
    )
    await db.commit()
