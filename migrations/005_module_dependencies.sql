-- Replace Neo4j graph database with a PostgreSQL table for module dependencies.
-- Recursive CTEs handle tree traversals that previously used Cypher.

CREATE TABLE IF NOT EXISTS module_dependencies (
    parent_repo     TEXT NOT NULL,
    parent_path     TEXT NOT NULL,
    parent_version  TEXT NOT NULL,
    dep_repo        TEXT NOT NULL,
    dep_path        TEXT NOT NULL,
    dep_version     TEXT NOT NULL,
    dep_name        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (parent_repo, parent_path, parent_version,
                 dep_repo, dep_path, dep_version)
);

CREATE INDEX IF NOT EXISTS idx_module_deps_parent
    ON module_dependencies (parent_repo, parent_path, parent_version);
CREATE INDEX IF NOT EXISTS idx_module_deps_dep_path
    ON module_dependencies (dep_path);
CREATE INDEX IF NOT EXISTS idx_module_deps_dep
    ON module_dependencies (dep_repo, dep_path, dep_version);
