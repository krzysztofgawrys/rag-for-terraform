-- Structured listening ports for each module, resolved deterministically from
-- the canonical security-group rules map (root module's `rules` variable) or
-- inline from_port literals - never name-as-heuristic, never LLM. Powers exact
-- port-match disambiguation among near-identical presets (e.g. "...on port 6379"
-- -> modules/redis), which neither the embedding nor the lexical signal can do
-- reliably. See app/core/ports.py.
ALTER TABLE modules ADD COLUMN IF NOT EXISTS ports int[];

CREATE INDEX IF NOT EXISTS modules_ports_idx ON modules USING GIN (ports);
