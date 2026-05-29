-- Add license column to modules table.
-- Populated during indexing by scanning LICENSE/COPYING files in repo root.
ALTER TABLE modules ADD COLUMN IF NOT EXISTS license TEXT;
