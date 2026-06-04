-- Full-text search vector for hybrid (semantic + lexical RRF) retrieval.
-- Generated STORED column stays in sync with description/name/path/tags/resources
-- automatically, so no re-index is needed for the lexical signal. to_tsvector
-- with an explicit 'english' config is IMMUTABLE, which a generated column requires.

-- NOTE: array_to_string() is STABLE (not IMMUTABLE), so it cannot appear in a
-- generated-column expression. The searchable text is therefore description +
-- module_name + module_path (the core search signal); tags/resources are folded
-- into the description by the indexer anyway.
ALTER TABLE modules
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(description, '') || ' ' ||
            coalesce(module_name, '') || ' ' ||
            replace(replace(coalesce(module_path, ''), '/', ' '), '-', ' ')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS modules_search_tsv_idx ON modules USING GIN (search_tsv);
