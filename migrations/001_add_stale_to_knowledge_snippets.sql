-- Add stale flag for self-evaluation quality gate.
-- When convention distillation fails quality check, the existing convention
-- is marked stale instead of being overwritten with bad data.
-- The retriever deprioritizes stale conventions in RAG prompts.

ALTER TABLE knowledge_snippets ADD COLUMN IF NOT EXISTS stale BOOLEAN DEFAULT FALSE;
