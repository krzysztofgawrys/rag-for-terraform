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
    UNIQUE (repo, module_path, version)
);

-- Vector index (cosine similarity)
CREATE INDEX IF NOT EXISTS modules_embedding_idx
    ON modules USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

-- Index on tags
CREATE INDEX IF NOT EXISTS modules_tags_idx ON modules USING GIN (tags);

-- Index on version
CREATE INDEX IF NOT EXISTS modules_version_idx ON modules (repo, module_path, version);

-- Indexing logs (for CI/CD)
CREATE TABLE IF NOT EXISTS index_jobs (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    repo        TEXT NOT NULL,
    repo_url    TEXT,
    commit_sha  TEXT,
    branch      TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending, running, done, failed
    triggered_by TEXT,                             -- webhook, manual, schedule
    git_tag     TEXT,                              -- git tag version (e.g. v1.2.0)
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error       TEXT,
    stats       JSONB DEFAULT '{}'                 -- modules_added, modules_updated, etc.
);

-- Query history (debug / analytics)
CREATE TABLE IF NOT EXISTS query_log (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    query       TEXT NOT NULL,
    query_type  TEXT,                              -- generate, optimize, audit
    results     JSONB,
    latency_ms  INT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- -- Existing database migration -----------------------------------------------
-- The following commands are idempotent (IF EXISTS / IF NOT EXISTS).
-- For fresh installations they do nothing (columns/indexes already exist above).
-- For older databases — they add missing columns and update constraints.

ALTER TABLE modules ADD COLUMN IF NOT EXISTS version TEXT NOT NULL DEFAULT 'latest';
ALTER TABLE modules DROP CONSTRAINT IF EXISTS modules_repo_module_path_key;
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'modules_repo_path_version_key'
    ) THEN
        ALTER TABLE modules ADD CONSTRAINT modules_repo_path_version_key
            UNIQUE (repo, module_path, version);
    END IF;
END $$;
ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS git_tag TEXT;
ALTER TABLE index_jobs ADD COLUMN IF NOT EXISTS repo_url TEXT;
ALTER TABLE modules ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES index_jobs(id) ON DELETE SET NULL;
ALTER TABLE modules ADD COLUMN IF NOT EXISTS code_hash TEXT;
CREATE INDEX IF NOT EXISTS modules_code_hash_idx ON modules (repo, module_path, code_hash);

-- -- Knowledge snippets (usage & convention embeddings) ----------------------
-- One row = one "knowledge sentence" (usage observation or distilled convention).
-- No raw HCL, no JSONB blobs — just text summaries + embeddings.

CREATE TABLE IF NOT EXISTS knowledge_snippets (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kind            TEXT NOT NULL,
        -- 'usage'              : single usage observation from a consumer repo
        -- 'convention.naming'  : naming convention distilled from usages
        -- 'convention.vars'    : variable usage patterns
        -- 'convention.codeploy': co-deployment patterns (stack)
        -- 'convention.tagging' : tagging convention
        -- 'convention.layout'  : file/directory layout
        -- 'convention.versions': version pinning policy
        -- 'stack_pattern'      : recurring multi-module deployment pattern
    module_ref      TEXT NOT NULL,          -- e.g. 'terraform-infrastructure-resources/vpc'
    scope           TEXT,                   -- 'env:prod', 'region:eu-west-1', 'global', NULL
    summary         TEXT NOT NULL,          -- natural language paragraph — this is what we embed
    evidence_count  INT  DEFAULT 1,         -- how many usages confirm this snippet (weight)
    source_locator  TEXT,                   -- 'consumer-repo@sha:path/file.tf:L42-L78'
    related_refs    TEXT[],                 -- links to other module_refs (for codeploy/stack)
    consumer_repo   TEXT,                   -- consumer repo name (for filtering/cleanup)
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

-- Partial unique index for convention upserts (one convention per module per dimension)
CREATE UNIQUE INDEX IF NOT EXISTS snippets_convention_upsert_idx
    ON knowledge_snippets (module_ref, kind)
    WHERE kind LIKE 'convention.%' OR kind = 'stack_pattern';

-- Consumer index jobs (tracks consumer repo indexing)
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

-- -- Audit logs -----------------------------------------------------------------
-- Full audit trail: API requests, MCP tool calls, Celery tasks, LLM prompts/responses.

CREATE TABLE IF NOT EXISTS audit_logs (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    category      TEXT NOT NULL,         -- 'api', 'mcp', 'worker', 'llm'
    action        TEXT NOT NULL,         -- e.g. 'POST /query/', 'tool:query_modules', 'task:index_repository', 'llm:acomplete'
    status        TEXT NOT NULL DEFAULT 'success',  -- 'success', 'error'
    duration_ms   INT,
    request_data  JSONB,                 -- full request body / prompt / task args
    response_data JSONB,                 -- full response / LLM output / task result
    error         TEXT,                  -- error message if status='error'
    metadata      JSONB DEFAULT '{}'     -- model name, tokens, caller, user-agent, etc.
);

CREATE INDEX IF NOT EXISTS audit_logs_created_at_idx ON audit_logs (created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_category_idx ON audit_logs (category, created_at DESC);
CREATE INDEX IF NOT EXISTS audit_logs_action_idx ON audit_logs (action);
