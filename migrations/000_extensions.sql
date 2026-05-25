-- Base schema for Terraform RAG.
--
-- On Docker, init_db.sql handles this via the entrypoint. On RDS/Aurora
-- there is no entrypoint — the migration runner is the first code that
-- touches the database, so we need the full schema here.
--
-- All statements are idempotent (IF NOT EXISTS / IF EXISTS).

-- Extensions
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Terraform modules with embeddings
CREATE TABLE IF NOT EXISTS modules (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    repo        TEXT NOT NULL,
    module_name TEXT NOT NULL,
    module_path TEXT NOT NULL,
    version     TEXT NOT NULL DEFAULT 'latest',
    tags        TEXT[] DEFAULT '{}',
    variables   JSONB DEFAULT '{}',
    outputs     JSONB DEFAULT '{}',
    resources   TEXT[] DEFAULT '{}',
    description TEXT,
    raw_code    TEXT,
    embedding   vector(768),
    indexed_at  TIMESTAMPTZ DEFAULT now(),
    commit_sha  TEXT,
    job_id      UUID,
    code_hash   TEXT,
    UNIQUE (repo, module_path, version)
);

CREATE INDEX IF NOT EXISTS modules_embedding_idx
    ON modules USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
CREATE INDEX IF NOT EXISTS modules_tags_idx ON modules USING GIN (tags);
CREATE INDEX IF NOT EXISTS modules_version_idx ON modules (repo, module_path, version);
CREATE INDEX IF NOT EXISTS modules_code_hash_idx ON modules (repo, module_path, code_hash);

-- Indexing jobs
CREATE TABLE IF NOT EXISTS index_jobs (
    id           UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    repo         TEXT NOT NULL,
    repo_url     TEXT,
    commit_sha   TEXT,
    branch       TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    triggered_by TEXT,
    git_tag      TEXT,
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ,
    error        TEXT,
    stats        JSONB DEFAULT '{}'
);

-- FK for modules.job_id (deferred — table may already exist without it)
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'modules_job_id_fkey'
    ) THEN
        ALTER TABLE modules ADD CONSTRAINT modules_job_id_fkey
            FOREIGN KEY (job_id) REFERENCES index_jobs(id) ON DELETE SET NULL;
    END IF;
END $$;

-- Query history
CREATE TABLE IF NOT EXISTS query_log (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query       TEXT NOT NULL,
    query_type  TEXT,
    results     JSONB,
    latency_ms  INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Knowledge snippets
CREATE TABLE IF NOT EXISTS knowledge_snippets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind            TEXT NOT NULL,
    module_ref      TEXT NOT NULL,
    scope           TEXT,
    summary         TEXT NOT NULL,
    evidence_count  INT  DEFAULT 1,
    source_locator  TEXT,
    related_refs    TEXT[],
    consumer_repo   TEXT,
    embedding       vector(768) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    updated_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS snippets_embedding_idx
    ON knowledge_snippets USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 200);
CREATE INDEX IF NOT EXISTS snippets_module_kind_idx
    ON knowledge_snippets (module_ref, kind);
CREATE INDEX IF NOT EXISTS snippets_related_refs_idx
    ON knowledge_snippets USING GIN (related_refs);
CREATE INDEX IF NOT EXISTS snippets_consumer_repo_idx
    ON knowledge_snippets (consumer_repo);
CREATE UNIQUE INDEX IF NOT EXISTS snippets_convention_upsert_idx
    ON knowledge_snippets (module_ref, kind)
    WHERE kind LIKE 'convention.%' OR kind = 'stack_pattern';

-- Consumer index jobs
CREATE TABLE IF NOT EXISTS consumer_index_jobs (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    repo            TEXT NOT NULL,
    repo_url        TEXT,
    branch          TEXT,
    commit_sha      TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    triggered_by    TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    error           TEXT,
    stats           JSONB DEFAULT '{}'
);

-- Audit logs
CREATE TABLE IF NOT EXISTS audit_logs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    category      TEXT NOT NULL,
    action        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'success',
    duration_ms   INT,
    request_data  JSONB,
    response_data JSONB,
    error         TEXT,
    metadata      JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS audit_logs_created_at_idx ON audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_category_idx ON audit_logs (category, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_action_idx ON audit_logs (action);
