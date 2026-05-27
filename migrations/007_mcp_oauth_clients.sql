-- Dynamic client registrations for MCP OAuth flow.
-- MCP clients self-register via POST /register; rows are durable across restarts.

CREATE TABLE IF NOT EXISTS mcp_oauth_clients (
    client_id       TEXT PRIMARY KEY,
    client_info     JSONB NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
