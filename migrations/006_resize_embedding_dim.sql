-- Migration 006: Resize embedding vector columns to match EMBEDDING_DIM setting.
--
-- Required when switching embedding provider (e.g. local 768-dim -> Bedrock Titan 1024-dim).
-- After this migration, ALL modules and knowledge_snippets must be re-indexed
-- because vectors of different dimensions are not comparable.
--
-- The {{EMBEDDING_DIM}} placeholder is replaced by the migration runner with the
-- actual value from settings before execution.

DO $$
DECLARE
    target_dim INT := {{EMBEDDING_DIM}};
    current_dim INT;
BEGIN
    -- Check current dimension from modules table (atttypmod stores dim for vector type)
    SELECT atttypmod INTO current_dim
    FROM pg_attribute
    WHERE attrelid = 'modules'::regclass
      AND attname = 'embedding';

    IF current_dim IS NOT NULL AND current_dim = target_dim THEN
        RAISE NOTICE 'Embedding dimension already %, nothing to do', target_dim;
        RETURN;
    END IF;

    RAISE NOTICE 'Resizing embedding columns from % to %', current_dim, target_dim;

    -- Drop IVFFlat indexes (cannot ALTER column with them present)
    DROP INDEX IF EXISTS modules_embedding_idx;
    DROP INDEX IF EXISTS snippets_embedding_idx;

    -- Null out existing embeddings (incompatible dimensions)
    UPDATE modules SET embedding = NULL WHERE embedding IS NOT NULL;
    UPDATE knowledge_snippets SET embedding = NULL WHERE embedding IS NOT NULL;

    -- Resize modules.embedding
    ALTER TABLE modules ALTER COLUMN embedding TYPE vector;
    EXECUTE format('ALTER TABLE modules ALTER COLUMN embedding TYPE vector(%s)', target_dim);

    -- Resize knowledge_snippets.embedding (has NOT NULL — drop temporarily)
    ALTER TABLE knowledge_snippets ALTER COLUMN embedding DROP NOT NULL;
    ALTER TABLE knowledge_snippets ALTER COLUMN embedding TYPE vector;
    EXECUTE format('ALTER TABLE knowledge_snippets ALTER COLUMN embedding TYPE vector(%s)', target_dim);
    -- NOT NULL intentionally not restored — existing rows are NULL until re-indexed

    -- Recreate IVFFlat indexes (empty until re-indexing)
    EXECUTE 'CREATE INDEX IF NOT EXISTS modules_embedding_idx ON modules USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)';
    EXECUTE 'CREATE INDEX IF NOT EXISTS snippets_embedding_idx ON knowledge_snippets USING ivfflat (embedding vector_cosine_ops) WITH (lists = 200)';

    RAISE NOTICE 'Embedding columns resized to vector(%)', target_dim;
END $$;
