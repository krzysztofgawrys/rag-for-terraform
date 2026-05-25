-- Add eval_score to knowledge_snippets for quality comparison across distillation runs
ALTER TABLE knowledge_snippets ADD COLUMN IF NOT EXISTS eval_score smallint;
