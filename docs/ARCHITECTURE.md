# Architecture and Technical Reference

Detailed technical documentation for Terraform RAG. For a high-level overview
and quick start, see the [README](../README.md).

---

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI 0.111 + uvicorn (async) |
| Vector DB | PostgreSQL 16 + pgvector (cosine similarity) |
| Dependency graph | PostgreSQL `module_dependencies` table + recursive CTEs |
| Task queue | Celery 5.4 + Redis 7 |
| Embeddings | `local`: sentence-transformers (CPU) or `bedrock`: Amazon Titan Embed V2 |
| LLM | Unified client - Anthropic SDK or OpenAI-compatible (default `claude-sonnet-4-6`) |
| Agent | Anthropic tool_use API or OpenAI-compatible function calling |
| HCL parser | python-hcl2 |
| Git | GitPython |
| MCP | FastMCP (`mcp[cli]`), mounted as Streamable HTTP in FastAPI |
| Frontend | TypeScript + Vite 5.4, D3 (graph), highlight.js (HCL syntax), marked (markdown) |
| Logging | structlog (structured JSON) |

## Directory Structure

```
app/
  main.py                 # FastAPI app, MCP mount, lifespan
  core/
    config.py             # Pydantic Settings, all env vars from .env
    parser.py             # HCL parser -> ParsedModule dataclass
    embeddings.py         # Embedding providers (local / Bedrock)
    consumer_parser.py    # Parses consumer repos: module{} blocks -> usage summaries
    vector_store.py       # pgvector: upsert, similarity_search, index_jobs, knowledge_snippets
    graph.py              # Dependencies: upsert, find_dependents, get_dependency_tree
    llm.py                # Unified LLM client (sync/async/streaming) with retry and audit
    audit.py              # Non-blocking audit logging (structlog + audit_logs table)
    migrations.py         # SQL migration runner (schema_migrations + advisory lock)
  services/
    agent.py              # Agentic tool-use loop: iterative LLM + tools for all query types
    indexer.py            # Pipeline: clone -> parse -> embed -> store (with code-hash cache)
    retriever.py          # RAG fallback pipeline (used when agent is disabled)
    consumer_indexer.py   # Pipeline: clone consumer repo -> parse -> usage snippets -> enqueue distillation
    convention_distiller.py  # Distils usage summaries -> convention snippets per dimension
    git_fetcher.py        # Fetch raw HCL from git on demand
  api/
    mcp_tools.py          # MCP server and tool definitions
    routes/               # REST API routes (index, query, modules, consumer, snippets, audit, auth)
  workers/
    celery_app.py         # Celery task definitions
  models/
    schemas.py            # Pydantic request/response schemas
frontend/
  index.html              # HTML shell - DOM structure for SPA
  src/
    main.ts               # App initialization, stats, router
    api.ts                # apiFetch() wrapper, toast(), escapeHtml()
    router.ts             # SPA router (DOM-based page switching)
    pages/                # Page modules (modules, query, jobs, graph, usage, knowledge, auditlogs)
    style.css             # Dark/light theme, responsive layout
migrations/               # Numbered SQL migration files (applied on startup)
scripts/                  # DB init, eval harness, utilities
.github/workflows/        # CI/CD automation
```

## Agentic Query Pipeline

When `AGENT_COMPOSE_ENABLED=true`, all query types use an iterative tool-use
loop instead of the single-shot pipeline.

### How it works

1. **Initial context** - seeded into the first user message:
   - Semantic search top-K modules (all types)
   - Module catalog + compose/stack patterns (compose only)
2. **Agent loop** (max `AGENT_MAX_TURNS` iterations):
   - LLM generates text and/or tool calls
   - Tool calls are executed against the knowledge base
   - Results are appended to conversation history
   - Loop continues until LLM stops calling tools
3. **SSE streaming** - events emitted to the frontend in real-time

### Available tools (6)

| Tool | Purpose |
|---|---|
| `list_modules` | Browse catalog with filters or semantic search |
| `get_module_details` | Full variables, outputs, resources, versions for a module |
| `get_dependencies` | Dependency tree + reverse dependents (recursive CTEs) |
| `get_module_usage` | Conventions (6 dimensions) + usage examples |
| `find_similar_usages` | Semantic search across usage/convention snippets |
| `fetch_example_code` | Raw HCL from git by source_locator |

### Query types

| Type | Purpose |
|---|---|
| **compose** | Generate HCL code for a component or full stack |
| **optimize** | Review code for version drift, convention violations, DRY |
| **audit** | Security and compliance review (IAM, encryption, networking) |
| **search** | Q&A, module comparison, usage guidance |

### Backend selection

- `LLM_BASE_URL` empty - Anthropic API (native `tool_use` blocks)
- `LLM_BASE_URL` set - OpenAI-compatible (function calling via LMStudio, Ollama, etc.)

The OpenAI path uses streaming so `reasoning_content` from reasoning models
(Qwen3) streams token-by-token to the UI.

### SSE event protocol

| Event | Payload | When |
|---|---|---|
| `sources` | `{sources: [...]}` | After initial semantic search |
| `agent_status` | `{message, turn, tool_calls}` | Start of each agent turn |
| `reasoning_start` | `{turn}` | Reasoning stream begins |
| `reasoning` | `{token, turn}` | Live reasoning token |
| `reasoning_end` | `{turn}` | Reasoning done |
| `tool_call` | `{tool, input, input_full, turn}` | Agent calls a tool |
| `tool_result` | `{tool, summary, detail, turn}` | Tool execution complete |
| `token` | `{token}` | Answer text chunk |
| `error` | `{message, latency_ms}` | Failure |
| `done` | `{latency_ms, ok, turns, tool_calls}` | Stream complete |

### Fallback

When `AGENT_COMPOSE_ENABLED=false` or no API key, the old pipeline in
`retriever.py` handles requests (shopping list - context assembly - single
LLM call).

## Knowledge Layer

Beyond the core module index, the system learns how modules are used in
practice by indexing consumer repos.

### Pipeline

1. **Consumer indexing** (`consumer_indexer.py`): clone a consumer repo - parse
   all `module {}` blocks - resolve against indexed modules - build usage
   summaries (template, no LLM) - embed - store as `kind='usage'` rows in
   `knowledge_snippets`.

2. **Convention distillation** (`convention_distiller.py`): takes all usage
   summaries for a module_ref - distils via LLM - produces one convention
   snippet per dimension.
   Dimensions: **naming, vars, codeploy, tagging, layout, versions**.
   Stored as `kind='convention.<dim>'` (upserted - one per module_ref per
   dimension). Includes self-evaluation quality gate (`eval_score` 1-5);
   conventions scoring below threshold are marked `stale=TRUE` and excluded
   from RAG prompts. Confidence labels: STRONG/MODERATE/WEAK/LOW_EVIDENCE.

3. **RAG injection**: in agent mode, the agent calls `get_module_usage` to
   fetch conventions on demand. In fallback mode, `retriever.py` injects them
   automatically.

4. **Compose-pattern indexing**: every `.tf` file in a consumer repo with
   2+ module calls gets a `kind='compose_pattern'` snippet describing the
   whole stack.

5. **Stack-pattern aggregation**: groups compose_patterns by their sorted
   `related_refs` set and emits one `kind='stack_pattern'` snippet per
   signature that appears in 2+ files.

### Database: `knowledge_snippets` table

| Column | Purpose |
|---|---|
| `module_ref` | `repo/module_path` for usage/convention; `compose:<repo>:<path>` for compose_pattern |
| `kind` | `'usage'` / `'convention.<dim>'` / `'compose_pattern'` / `'stack_pattern'` |
| `summary` | Text content |
| `evidence_count` | How many deployments support this convention |
| `eval_score` | Self-evaluation quality score (1-5) |
| `stale` | `TRUE` when convention failed quality gate |
| `source_locator` | `consumer-repo@<sha7>:path/file.tf` (no line range) |
| `embedding` | `vector(N)` for semantic search (768 local / 1024 Bedrock) |

### Key rule: conventions are authoritative

All agent system prompts and the fallback retriever treat conventions as
authoritative - they override generic Terraform best practices.

## MCP Server

Mounted as Streamable HTTP in FastAPI at `/mcp/`.

Available tools:

| Tool | Description |
|---|---|
| `query_modules` | Full RAG context for external code generation (no final LLM call) |
| `pick_modules` | Cheap Haiku call returns a shopping list of module references |
| `list_modules` | Browse modules with filters or semantic search |
| `get_module_details` | Full variables, outputs, resources, versions |
| `get_dependencies` | Dependency tree + reverse dependents |
| `get_module_usage` | Conventions + usage examples for a module_ref |
| `find_similar_usages` | Semantic search across usage snippets |
| `fetch_example_code` | Fetch raw HCL from git by source_locator |
| `get_stats` | Knowledge base statistics |

All tools are decorated with `@audit_mcp_tool` for automatic audit logging.

## API Endpoints

### Index
| Method | Path | Description |
|---|---|---|
| POST | `/index/` | Create indexing job (202 Accepted, Celery task) |
| POST | `/index/{job_id}/reindex` | Re-run with previous parameters |
| DELETE | `/index/{job_id}` | Delete job and all indexed modules |
| GET | `/index/{job_id}` | Job status |
| GET | `/index/` | List jobs |

### Query
| Method | Path | Description |
|---|---|---|
| POST | `/query/` | RAG query (compose/optimize/audit/search) |
| POST | `/query/stream` | SSE streaming RAG query |
| POST | `/query/dependencies` | Dependency tree |
| GET | `/query/stats` | Knowledge base stats |
| POST | `/query/eval` | Retrieval evaluation |

### Modules
| Method | Path | Description |
|---|---|---|
| GET | `/modules/` | List modules with filters |
| GET | `/modules/versions/all` | All available versions |
| GET | `/modules/tags/all` | All tags with counts |
| GET | `/modules/{repo}/{path}/versions` | Module version history |
| GET | `/modules/{repo}/{path}/dependencies` | Module dependency tree |
| GET | `/modules/{repo}/{path}/dependents` | Reverse dependents |

### Consumer
| Method | Path | Description |
|---|---|---|
| POST | `/consumer/` | Index consumer repo |
| GET | `/consumer/` | List consumer index jobs |
| POST | `/consumer/{job_id}/reindex` | Re-run with previous parameters |
| DELETE | `/consumer/{job_id}` | Delete job and cascading cleanup |
| POST | `/consumer/distill` | Trigger convention distillation |

### Knowledge
| Method | Path | Description |
|---|---|---|
| GET | `/snippets/module-refs` | Modules with snippet counts |
| GET | `/snippets/consumer-repos` | Consumer repos |
| GET | `/snippets/module-refs/{ref}` | Conventions + usage for a module |

### Audit
| Method | Path | Description |
|---|---|---|
| GET | `/audit/` | Browse logs with filters and pagination |
| GET | `/audit/stats` | Summary counts by category/status |

### Webhooks
| Method | Path | Description |
|---|---|---|
| POST | `/webhook/github` | Push/tag events (HMAC-SHA256) |
| POST | `/webhook/gitlab` | Push events (token verify) |

### Auth
| Method | Path | Description |
|---|---|---|
| GET | `/auth/info` | Auth mode and SSO config |
| POST | `/auth/login` | Local login (returns JWT) |
| POST | `/auth/refresh` | Refresh access token |
| GET | `/auth/me` | Current user info |
| POST | `/auth/logout` | Logout |

Full Swagger documentation at `/docs` when the API is running.

## Embedding Providers

| Provider | Model | Dimensions | Needs PyTorch | Auth |
|---|---|---|---|---|
| `local` (default) | sentence-transformers | 768 | Yes (~2GB RAM) | None |
| `bedrock` | Amazon Titan Embed V2 | 1024 | No | IAM |

When switching providers with a different dimension, migration `006` resizes
the `vector(N)` columns automatically. All modules and snippets must be
re-indexed after switching.

Docker build: pass `--build-arg EMBEDDING_PROVIDER=bedrock` to skip PyTorch
and model download (~2GB savings).

## Configuration Reference

Key environment variables (see `.env.example` for the full list):

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_PROVIDER` | `local` | `local` or `bedrock` |
| `LLM_MODEL` | `claude-sonnet-4-6` | Model for queries |
| `LLM_BASE_URL` | _(empty)_ | Empty = Anthropic, or OpenAI-compatible URL |
| `LLM_THINKING_BUDGET` | `8192` | Anthropic extended thinking tokens (0 = disable) |
| `LLM_MAX_RETRIES` | `3` | Retries on transient errors (429, 500, 529) |
| `AGENT_COMPOSE_ENABLED` | `false` | Enable agentic tool-use loop |
| `AGENT_MAX_TURNS` | `15` | Max agent iterations |
| `AGENT_MODEL` | _(empty)_ | Override model for agent (defaults to `LLM_MODEL`) |
| `DESCRIPTION_LLM_MODEL` | _(empty)_ | Cheap model for module descriptions during indexing |
| `AUDIT_LOG_ENABLED` | `true` | Enable/disable audit logging |
| `AUDIT_LOG_LLM_PROMPTS` | `false` | `true` stores full prompt/response text in audit logs |
| `AUTH_MODE` | `disabled` | `disabled`, `local`, or `sso` |

## Database Schema

### Tables

| Table | Purpose |
|---|---|
| `modules` | Main table with `vector(N)` embedding column |
| `module_dependencies` | Dependency edges between modules (parent -> child) |
| `index_jobs` | Indexing history (pending/running/done/failed) |
| `consumer_index_jobs` | Consumer repo indexing history |
| `knowledge_snippets` | Usage observations and convention snippets |
| `query_log` | Optional query logging |
| `audit_logs` | Audit trail (category, action, status, duration, metadata) |
| `schema_migrations` | Tracks applied SQL migrations |

### Indexes

- IVFFlat with `lists=100` on `modules.embedding`
- IVFFlat with `lists=200` on `knowledge_snippets.embedding`
- For >10k vectors, increase lists to `sqrt(row_count)` or switch to HNSW

### Migrations

Schema changes go in `migrations/` as numbered SQL files. The migration runner
applies them automatically on API startup with advisory lock concurrency.
Do not put new schema in `scripts/init_db.sql` - that is for initial setup only.

## CI/CD Integration

### GitHub Actions

Copy `.github/workflows/rag-index.yml` into your module repos. On pushes to
`main` that change `.tf` files, it triggers re-indexing automatically.

Required secrets: `RAG_BACKEND_URL`, `RAG_API_KEY`.

### Webhooks

| Endpoint | Verification |
|---|---|
| `POST /webhook/github` | HMAC-SHA256 (`GITHUB_WEBHOOK_SECRET`) |
| `POST /webhook/gitlab` | Token header (`GITLAB_WEBHOOK_TOKEN`) |

## Retrieval Evaluation

A fixture-based evaluation harness measures retrieval quality:

```bash
# CLI (add --token <trag_* key> when auth is enabled)
python scripts/eval_retrieval.py --url http://localhost:8000

# JSON output
python scripts/eval_retrieval.py --json

# API endpoint
curl -X POST http://localhost:8000/query/eval
```

`scripts/eval_queries.yaml` holds the baseline cases (query, expected
`module_ref`s, `any`/`all` match - reports hit rate and module recall). The
pure scoring core in `scripts/eval_scoring.py` adds rank-aware fields that catch
bad ranking, not just recall:

- `top_rank` - the expected ref must appear within the first N (not merely
  somewhere in top-K), so near-duplicate disambiguation is actually measured
- `forbidden_refs` - refs that must NOT appear in the rank window (the testable
  form of "a deprecated/legacy module must not outrank the current one")
- reciprocal rank / MRR for rank-aware aggregate reporting

`scripts/eval_queries_adversarial.yaml` exercises these against real
near-duplicate clusters; the report adds MRR and any forbidden-ref violations.

## Testing

A pytest suite (zero mocks - pure functions tested directly, DB logic against a
real Postgres) covers the deterministic core:

| Area | Covers |
|---|---|
| Parser | `parse_module` golden + `_extract_tags` edge cases (pinned to hcl2 4.3.2) |
| Consumer parser | source/version resolution, usage + compose summaries, end-to-end golden |
| Graph | recursive-CTE forward/reverse traversal + cycle termination (real Postgres) |
| Distiller | `extract_assessment` / `strip_preamble` parsing |
| Migrations | advisory-lock runner: ordering, idempotency, comment-only, concurrency |
| Vector store | `similarity_search` ranking + latest-version-wins + filters; code-hash cache |
| Auth | JWT lifecycle, forgery/audience/expiry rejection, API-key hashing |
| Webhook / SSRF | GitHub HMAC accept/reject; clone-URL allowlist + metadata blocking |
| Eval scoring | rank-aware scoring (`forbidden_refs`, `top_rank`, MRR) |

```bash
# needs the app dependencies + the pytest stack; hcl2 stays pinned at 4.3.2
pip install -r requirements.txt pytest pytest-asyncio pytest-timeout
POSTGRES_HOST=127.0.0.1 POSTGRES_PORT=5432 POSTGRES_USER=postgres \
POSTGRES_PASSWORD=postgres POSTGRES_DB=ragtest pytest tests/ -v
```

DB-backed tests skip cleanly when no Postgres is reachable. The agent loop, LLM
distiller, and model embeddings are deliberately NOT unit-tested -
non-deterministic output makes mock tests test the mock; use the eval harness
plus a recall/MRR threshold for those instead.

## Frontend Development

```bash
cd frontend
npm install
npm run dev    # Vite dev server at :3000, proxies API to :8000
```

## Production Deployment

A production Docker Compose file (`docker-compose.prod.yml`) is included.
It does not expose database/Redis ports to the host and does not use
bind-mount volumes.

## Security

- **Agent tools are read-only** over the indexed DB - a prompt-injected agent
  can read but cannot write or shell out. This is the single largest piece of
  risk reduction in the design; keep it that way.
- **Auth**: API keys (`trag_*`, SHA-256 hashed), local JWT (HS256, audience-
  validated, refused to boot on the default secret), or ALB-terminated SSO.
  SSO group claims are read only from a signature-verified Cognito token.
- **Clone SSRF**: every clone path (webhook, `/index/`, reindex, consumer)
  validates the repo host against an allowlist and blocks loopback / RFC-1918 /
  cloud-metadata addresses.
- **Webhooks**: GitHub HMAC-SHA256 is enforced (no silent bypass when unset);
  GitLab uses a token header; both check the clone-host allowlist.
- **Frontend**: streamed LLM/markdown output is rendered through DOMPurify;
  anti-clickjacking/MIME headers are set, with CSP and Cloudflare in front of
  the hosted demo.
- **Audit**: every MCP tool, LLM call, and significant API action is logged;
  prompt/response text is redacted by default (`AUDIT_LOG_LLM_PROMPTS=false`).

## Known Limitations

### Convention authority vs correctness

Distilled conventions are authoritative in RAG prompts. This works well when
usage is healthy, but if many repos use a module incorrectly, the distiller
will faithfully capture that pattern and the agent will recommend it with
confidence. The self-evaluation checks faithfulness to source data, not whether
the pattern itself is correct.

Mitigations: confidence labels (STRONG/MODERATE/WEAK/LOW_EVIDENCE), `stale`
flag for low-scoring conventions, `eval_reason` for manual review. A proper
fix requires an external correctness signal (security policies, version checks,
human review).

### Convention distillation timing

Convention distillation runs automatically as part of consumer indexing
(`run_distillation=true`, the default): re-indexing a consumer repo redistils
the conventions for the modules it touches. A manual `POST /consumer/distill`
remains available for ad-hoc runs, and
`.github/workflows/rag-consumer-index.yml.example` shows wiring it into a
consumer repo's CI. (Distillation does not yet run on a standalone schedule -
it is tied to consumer-repo indexing events.)

### Vector space scaling

Every module version gets its own embedding row. Code-hash caching avoids
redundant calls but does not reduce stored vectors. For repos with many tags,
vector count grows with `modules x versions`. No automatic pruning of old
versions. pgvector IVFFlat indexes may need tuning above 10k vectors.
