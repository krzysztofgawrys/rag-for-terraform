-- Auth tables: users, api_keys, and audit_logs user tracking

CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    external_id   TEXT UNIQUE,                 -- SSO sub claim (NULL for local users)
    email         TEXT UNIQUE NOT NULL,
    display_name  TEXT,
    password_hash TEXT,                         -- only for local auth mode
    role          TEXT NOT NULL DEFAULT 'user', -- 'admin', 'user', 'readonly'
    sso_groups    TEXT[] DEFAULT '{}',
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL,         -- SHA-256 of the full key
    key_prefix  TEXT NOT NULL,         -- first 8 chars for display
    role        TEXT NOT NULL DEFAULT 'user',
    expires_at  TIMESTAMPTZ,
    last_used   TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys (key_hash);

ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS user_id UUID REFERENCES users(id);
ALTER TABLE audit_logs ADD COLUMN IF NOT EXISTS user_email TEXT;
