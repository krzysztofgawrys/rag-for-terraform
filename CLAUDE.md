# CLAUDE.md — Terraform RAG Backend

File for Claude Code. Contains project context, conventions, and pitfalls.

---

## What is this project

RAG (Retrieval-Augmented Generation) backend for managing knowledge about
Terraform modules spread across multiple repositories. Features:

- **Indexing** repositories with Terraform modules (HCL parsing, embeddings,
  storage in pgvector)
- **Agentic query pipeline** — iterative tool-use loop where the LLM
  autonomously explores the knowledge base (browse modules, check details,
  read conventions, fetch example code) before composing an answer
- **Dependency analysis** between modules (PostgreSQL recursive CTEs)
- **Versioning** of modules through git tag discovery
- **CI/CD integration** via GitHub/GitLab webhooks and GitHub Actions
- **MCP server** — tools accessible directly from Claude Code
- **Knowledge layer** — consumer repo indexing, convention distillation, usage patterns
- **Audit logging** — non-blocking event logging to PostgreSQL (API, MCP, LLM, worker)
- **Authentication** — SSO (ALB OIDC) or local email/password with JWT

---

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI 0.111 + uvicorn (async) |
| Vector database | PostgreSQL 16 + pgvector (cosine similarity) |
| Dependency graph | PostgreSQL `module_dependencies` table + recursive CTEs |
| Task queue | Celery 5.4 + Redis 7 |
| Embeddings | `local`: sentence-transformers (CPU) or `bedrock`: Amazon Titan Embed V2 (API) |
| LLM | Unified client — Anthropic SDK or OpenAI-compatible (default `claude-sonnet-4-6`) |
| Agent | Anthropic tool_use API or OpenAI-compatible function calling (same backend selection as LLM) |
| HCL parser | python-hcl2 |
| Git | GitPython |
| MCP | FastMCP (`mcp[cli]`), mounted as Streamable HTTP in FastAPI |
| Frontend | TypeScript + Vite 5.4, D3 (graph), highlight.js (HCL syntax), marked (markdown) — served by nginx |
| Logging | structlog (structured JSON) |

---

## Directory structure

```
app/
  main.py              # FastAPI app, CORS, router includes, MCP mount, lifespan
  core/
    config.py          # Pydantic Settings, all env vars from .env
    parser.py          # HCL parser → ParsedModule dataclass
    embeddings.py      # sentence-transformers, embed_module() / embed_query()
    consumer_parser.py # Parses consumer repos: finds module{} blocks, builds usage summaries
    vector_store.py    # pgvector: upsert, similarity_search, index_jobs, knowledge_snippets
    graph.py           # Dependencies: upsert_module, find_dependents, get_dependency_tree (PostgreSQL)
    llm.py             # Unified LLM client (sync/async/streaming) with retry & audit
    audit.py           # Non-blocking audit logging (emit/emit_sync → structlog + audit_logs table)
    migrations.py      # Lightweight SQL migration runner (schema_migrations table + advisory lock)
  services/
    agent.py                # Agentic tool-use loop: iterative LLM + tools for all query types
    indexer.py              # Pipeline: clone → parse → embed → store (with code-hash cache)
    retriever.py            # RAG fallback pipeline (used when agent is disabled) + shared helpers
    consumer_indexer.py     # Pipeline: clone consumer repo → parse module{} blocks → usage snippets → enqueue distillation
    convention_distiller.py # Distills N usage summaries → convention snippets per dimension (naming, vars, codeploy, tagging, layout, versions)
    git_fetcher.py          # Fetch raw HCL from git on demand (for MCP fetch_example_code)
  api/
    mcp_tools.py       # FastMCP server: query_modules, list_modules, get_module_details, get_dependencies, get_module_usage, find_similar_usages, fetch_example_code, get_stats
    routes/
      webhook.py       # POST /webhook/github, /webhook/gitlab (HMAC verify)
      index.py         # POST /index/, DELETE /index/{job_id}, POST /index/{job_id}/reindex, GET /index/{job_id}, GET /index/
      query.py         # POST /query/, POST /query/stream (SSE), POST /query/dependencies, GET /query/stats, POST /query/eval
      modules.py       # GET /modules/, /modules/versions/all, /modules/tags/all, /modules/{repo}/{path}/versions, /modules/{repo}/{path}/dependencies, /modules/{repo}/{path}/dependents
      consumer.py      # POST /consumer/ (index consumer repo), GET /consumer/ (list jobs), POST /distill, etc.
      snippets.py      # GET /snippets/module-refs, /snippets/consumer-repos, /snippets/module-refs/{ref}
      audit.py         # GET /audit/ (browse logs), GET /audit/stats
  workers/
    celery_app.py      # Celery task: index_repository_task (max 2 retries, 30s countdown)
  models/
    schemas.py         # Pydantic request/response models
frontend/
  index.html           # HTML shell — DOM structure for SPA
  package.json         # Deps: d3, highlight.js, marked; Dev: vite, typescript
  vite.config.ts       # Dev server :3000 with proxy to API :8000
  tsconfig.json        # ES2020, strict, bundler resolution
  Dockerfile           # Build stage (Vite) + nginx serve
  src/
    main.ts            # App initialization, stats, router
    api.ts             # apiFetch() wrapper, toast(), escapeHtml()
    router.ts          # SPA router (DOM-based page switching)
    types.ts           # TS interfaces: Module, Stats, QueryType, etc.
    hcl-lang.ts        # HCL language definition for highlight.js
    style.css          # Dark/light theme (toggle), IBM Plex Mono/Sans, responsive grid
    pages/
      modules.ts       # Module list, filtering, detail panel
      query.ts         # Query form, streaming response, agent UI (reasoning panels, tool indicators)
      jobs.ts          # Index job management
      graph.ts         # D3 force-directed dependency graph
      usage.ts         # Consumer index job management (polling, reindex, delete)
      knowledge.ts     # Knowledge browser — conventions, usage examples per module
      auditlogs.ts     # Audit log browser with filters, pagination, detail panel
migrations/
  001_*.sql            # Numbered SQL migration files (applied on app startup)
scripts/
  init_db.sql          # Initial pgvector schema (runs once via Docker entrypoint)
  eval_retrieval.py    # Retrieval evaluation harness (CLI + JSON output)
  eval_queries.yaml    # YAML fixture: queries + expected module_refs
.github/
  workflows/
    rag-index.yml      # GitHub Actions: auto-index on push of .tf files to main
.mcp.json              # MCP config for Claude Code: http://localhost:8000/mcp/
docker-compose.yml     # Orchestration: api, worker, postgres, redis, frontend
Dockerfile             # Python 3.12, PyTorch CPU-only, requirements
.env.example           # Environment variable template
```

---

## Key conventions

### LLM client (`app/core/llm.py`)
Always use `llm.complete(prompt, system, max_tokens)` (sync) or
`llm.acomplete(...)` (async) instead of directly calling `anthropic.Anthropic()`
or `openai.OpenAI()`. For streaming: `llm.astream(prompt, system, max_tokens)`.

**Exception**: `app/services/agent.py` uses Anthropic/OpenAI SDK directly
because the tool-use API requires specific parameters (`tools`, `tool_choice`)
that `llm.py` abstractions don't support. This is intentional.

The client automatically selects the backend based on `settings.llm_base_url`:
- empty → Anthropic SDK
- `https://openrouter.ai/api/v1` → OpenAI-compatible
- `http://ollama:11434/v1` → Ollama locally

Always handle the fallback when `llm.complete()` returns `""` (missing API key).

**Retry logic**: transient errors (429, 500, 529) are retried up to
`llm_max_retries` times with exponential backoff (1s, 2s, 4s).

**Extended thinking**: when using Anthropic SDK and `llm_thinking_budget > 0`,
the client sends `thinking.budget_tokens`. Set to 0 to disable.

**Audit integration**: `acomplete()` and `adescribe()` emit audit events
(category `llm`) with duration, request/response data.

### Separate LLM for module descriptions
`llm.describe()` uses a separate configuration (`description_llm_model`,
`description_llm_base_url`, `description_llm_api_key`). If empty — falls back
to the main LLM. This allows using a cheap model for descriptions during
indexing and a more powerful one for queries.

### Pydantic Settings (`app/core/config.py`)
`extra = "ignore"` — unknown variables in `.env` are ignored instead of raising
`extra_forbidden`. Do not remove this.

### Async everywhere
All DB functions (`vector_store.py`, `graph.py`) are async and use the shared
`AsyncSessionLocal` from `vector_store.py`. In Celery tasks (`celery_app.py`)
we use `asyncio.run()` as a bridge. `make_session_factory()` creates a fresh
engine per task (avoids async event loop errors).

### ParsedModule dataclass
Central data model from `parser.py`. All services operate on this type.
Do not add fields to `ParsedModule` without updating the SQL schema.

### Module chunking
One chunk = one Terraform module (directory with `.tf` files), not a file.
Embeddings are built from a combination of: LLM description + metadata + code
snippet (max 5000 chars). The embedding text is the same regardless of provider.

### Code-hash caching (indexer)
The indexer computes an MD5 hash of the module's code. If an identical hash
already exists in the database, it reuses the description and embedding instead
of calling the LLM/embedding model again. Significantly speeds up re-indexing.

### Concurrent LLM calls
`settings.llm_concurrent_prompts` (default 1) controls how many parallel LLM
calls the indexer makes. For local models (LMStudio, Ollama) set to e.g. 4.

### Embedding providers (`app/core/embeddings.py`)

Two providers, selected by `EMBEDDING_PROVIDER` setting:

| Provider | Model | Dim | Needs PyTorch | Auth |
|---|---|---|---|---|
| `local` (default) | sentence-transformers (configurable) | 768 | Yes (~2GB RAM) | None |
| `bedrock` | Amazon Titan Embed V2 | 1024 | No | IAM |

When switching providers with a different dimension, migration `006` resizes
the `vector(N)` columns automatically. **All modules and snippets must be
re-indexed after switching** (old embeddings are nulled out).

Docker build: pass `--build-arg EMBEDDING_PROVIDER=bedrock` to skip PyTorch
and model download stages (saves ~2GB image size + RAM).

### Notable config settings (`app/core/config.py`)
- `embedding_provider` (default `"local"`) — `"local"` or `"bedrock"`
- `embedding_bedrock_model_id` (default `"amazon.titan-embed-text-v2:0"`) — Bedrock model
- `embedding_bedrock_region` — AWS region (falls back to `aws_bedrock_region`)
- `llm_thinking_budget` (default 8192) — Anthropic extended thinking budget (0 = disabled)
- `llm_max_retries` (default 3) — max retries on transient API errors (429, 500, 529)
- `agent_compose_enabled` (default False) — enable agentic tool-use loop for all query types
- `agent_max_turns` (default 15) — max agent iterations before forced stop
- `agent_model` (default empty = use `llm_model`) — model override for agent loop
- `audit_log_enabled` (default True) — enable/disable audit logging
- `audit_log_llm_prompts` (default True) — False redacts prompt/response text in audit logs

### Structured logging
The project uses `structlog` (JSON). Import with `log = structlog.get_logger()`.

### Audit logging (`app/core/audit.py`)
All significant system events are logged to the `audit_logs` table:
- **Categories**: `api`, `mcp`, `llm`, `worker`
- `emit()` (async) and `emit_sync()` (sync) — non-blocking, fire-and-forget DB writes
- `@audit_mcp_tool` decorator wraps all MCP tool functions automatically
- LLM prompts can be redacted via `audit_log_llm_prompts = False` (keeps only lengths)
- Controlled by `audit_log_enabled` setting (default `True`)

Never call `emit()` inside Celery tasks — use `emit_sync()` instead.

### Database migrations
Schema changes go in `migrations/` as numbered SQL files (`001_description.sql`,
`002_description.sql`, ...). The migration runner (`app/core/migrations.py`) applies
them automatically on API startup with an advisory lock for concurrency safety.

**Do NOT** put new schema changes in `scripts/init_db.sql` — that file is only for
the initial schema on fresh databases. All subsequent changes must be migrations.

To add a new migration:
1. Create `migrations/NNN_short_description.sql` (next number in sequence)
2. Write idempotent SQL (use `IF NOT EXISTS`, `IF EXISTS`)
3. Restart the API — migration applies automatically

---

## Agentic Query Pipeline (`app/services/agent.py`)

When `AGENT_COMPOSE_ENABLED=true`, **all query types** (compose, optimize,
audit, search) use an iterative tool-use loop instead of the old single-shot
pipeline. The agent autonomously explores the knowledge base before composing
an answer.

### How it works

1. **Initial context** — seeded into the first user message:
   - Semantic search top-K modules (all types)
   - Module catalog + compose/stack patterns (compose only)
2. **Agent loop** (max `AGENT_MAX_TURNS` iterations):
   - LLM generates text and/or tool calls
   - Tool calls are executed against the knowledge base
   - Results are appended to conversation history
   - Loop continues until LLM stops calling tools
3. **SSE streaming** — events emitted to the frontend in real-time

### Backend selection

Same convention as `llm.py`:
- `LLM_BASE_URL` empty → Anthropic API (native `tool_use` blocks)
- `LLM_BASE_URL` set → OpenAI-compatible (function calling, e.g. LMStudio/Ollama)

The OpenAI path uses **streaming** (`stream=True`) so `reasoning_content`
from reasoning models (Qwen3) streams token-by-token to the UI.

### Available tools (6)

| Tool | Purpose |
|---|---|
| `list_modules` | Browse catalog with filters or **semantic search** (`semantic_query` param) |
| `get_module_details` | Full variables, outputs, resources, versions for a module |
| `get_dependencies` | Dependency tree + reverse dependents (recursive CTEs) |
| `get_module_usage` | Conventions (6 dimensions) + usage examples |
| `find_similar_usages` | Semantic search across usage/convention snippets |
| `fetch_example_code` | Raw HCL from git by source_locator |

Tools are the same async functions from `mcp_tools.py` — called directly
(not via MCP HTTP). The `@audit_mcp_tool` decorator fires on every call.

### Per-type system prompts

Each query type has a dedicated system prompt in `AGENT_SYSTEM_PROMPTS`:
- **compose** — HCL code generation, module composition rules, format rules
- **optimize** — code review, version pinning, convention drift, security
- **audit** — security/compliance, IAM, encryption, network, secrets, tagging
- **search** — knowledge Q&A, module comparison, usage guidance

All share a common `_TOOL_PREAMBLE` describing the available tools.

### UI filter constraints

When the user sets repo/tag/version filters in the UI, they are appended to
the user message as a `SCOPE CONSTRAINT` block. The agent respects these in
subsequent tool calls.

### SSE event protocol

| Event | Payload | When |
|---|---|---|
| `sources` | `{sources: [...]}` | After initial semantic search |
| `agent_status` | `{message, turn, tool_calls}` | Start of each agent turn |
| `reasoning_start` | `{turn}` | Reasoning stream begins (opens collapsible panel) |
| `reasoning` | `{token, turn}` | Live reasoning token (streamed from model) |
| `reasoning_end` | `{turn}` | Reasoning done (auto-collapses panel) |
| `tool_call` | `{tool, input, input_full, turn}` | Agent calls a tool |
| `tool_result` | `{tool, summary, detail, turn}` | Tool execution complete |
| `token` | `{token}` | Answer text chunk |
| `error` | `{message, latency_ms}` | Failure |
| `done` | `{latency_ms, ok, turns, tool_calls}` | Stream complete |

### Fallback

When `AGENT_COMPOSE_ENABLED=false` or no API key, the old pipeline in
`retriever.py` handles requests (shopping list → context assembly → single
LLM call). The old pipeline is fully preserved as fallback.

---

## Knowledge Layer (conventions & usage)

Beyond the core module index, the system learns **how modules are actually used**
across the organisation by indexing "consumer" repos.

### Pipeline

1. **Consumer indexing** (`consumer_indexer.py`): clone a consumer repo → parse
   all `module {}` blocks → resolve against indexed modules → build usage
   summaries (template, no LLM) → embed → store as `kind='usage'` rows in
   `knowledge_snippets`.

2. **Convention distillation** (`convention_distiller.py`): takes all usage
   summaries for a module_ref → distills via cheap LLM → produces one
   convention snippet per dimension.
   Dimensions: **naming, vars, codeploy, tagging, layout, versions**.
   Stored as `kind='convention.<dim>'` (upserted — one per module_ref per
   dimension). Includes self-evaluation quality gate (`eval_score` 1-5);
   conventions scoring below threshold are marked `stale=TRUE` and excluded
   from RAG prompts. Confidence labels: STRONG/MODERATE/WEAK/LOW_EVIDENCE.

3. **RAG injection**: in agent mode, the agent calls `get_module_usage` to
   fetch conventions on demand. In fallback mode, `retriever.py →
   _get_snippet_context()` injects them automatically.

4. **Compose-pattern indexing**: every `.tf` file in a consumer repo with
   **2+ module calls** gets a `kind='compose_pattern'` snippet describing the
   whole stack (file path, module list, instance names, versions).

5. **Stack-pattern aggregation**: `recompute_stack_patterns()` groups
   compose_patterns by their sorted `related_refs` set and emits one
   `kind='stack_pattern'` snippet per signature that appears in 2+ files.

### Database: `knowledge_snippets` table

| Column | Purpose |
|---|---|
| `module_ref` | `repo/module_path` for usage/convention; `compose:<repo>:<path>` for compose_pattern; `stack:<sig>` for stack_pattern |
| `kind` | `'usage'` / `'convention.<dim>'` / `'compose_pattern'` / `'stack_pattern'` |
| `summary` | Text content (usage observation, distilled convention, or stack description) |
| `evidence_count` | How many deployments support this convention |
| `eval_score` | Self-evaluation quality score (1-5, set by distiller) |
| `stale` | `TRUE` when convention failed quality gate — excluded from RAG injection |
| `source_locator` | `consumer-repo@sha:path/file.tf:L42-L78` |
| `consumer_repo` | Consumer repo name (for filtering/cleanup) |
| `embedding` | `vector(768)` for semantic search |
| `related_refs` | Array of related module_refs |

Unique partial index on `(module_ref, kind)` WHERE `kind LIKE 'convention.%'`.

### Key rule: conventions are authoritative

All agent system prompts and the fallback retriever treat conventions as
**authoritative** — they override generic Terraform best practices. When
modifying prompts, always preserve this priority.

### Query types

| Type | When to use | Agent behaviour |
|---|---|---|
| `compose` | Generate HCL code (single component or full multi-module stack) | Full initial context (catalog + patterns), calls `get_module_details` + `get_module_usage`, produces HCL |
| `optimize` | Review existing code for improvements | Checks latest versions, convention drift, security, DRY violations via tools |
| `audit` | Security & compliance review | Inspects resources, dependencies, conventions for security issues |
| `search` | Q&A over the knowledge base | Browses modules, fetches details, compares alternatives |

**Note**: `generate` is a legacy alias for `compose` (accepted by the API,
removed from UI).

---

## MCP Server

MCP server integrated directly in FastAPI (`app/api/mcp_tools.py`).
Mounted as Streamable HTTP in `app/main.py`:
```python
mcp_http_app = mcp_tools.mcp.streamable_http_app()
app.mount("", mcp_http_app)
```

Claude Code configuration (`.mcp.json`):
```json
{"mcpServers": {"terraform-rag": {"type": "http", "url": "http://localhost:8000/mcp/"}}}
```

Available MCP tools:
| Tool | Description |
|---|---|
| `query_modules` | Full RAG context for external code generation (does NOT call the final LLM). |
| `pick_modules` | Cheap Haiku call returns a shopping list of `repo//module_path` refs. |
| `list_modules` | Browse modules with filters (repo, tag, resource_type, search) or **semantic search** (`semantic_query` param for natural language similarity). |
| `get_module_details` | Full module info: variables, outputs, resources, versions |
| `get_dependencies` | Dependency tree + reverse dependents |
| `get_module_usage` | Conventions + usage examples for a module_ref (with staleness markers) |
| `find_similar_usages` | Semantic search across usage knowledge snippets |
| `fetch_example_code` | Fetch raw HCL from git by source_locator |
| `get_stats` | Knowledge base statistics |

All tools are decorated with `@audit_mcp_tool` for automatic audit logging.

---

## API Endpoints

### Index
| Method | Path | Description |
|---|---|---|
| POST | `/index/` | Create indexing job (202 Accepted, Celery task) |
| POST | `/index/{job_id}/reindex` | Re-run indexing with previous parameters |
| DELETE | `/index/{job_id}` | Delete job and all indexed modules |
| GET | `/index/{job_id}` | Job status |
| GET | `/index/` | List jobs (optional repo filter) |

### Query
| Method | Path | Description |
|---|---|---|
| POST | `/query/` | RAG query (query_type: compose/optimize/audit/search) |
| POST | `/query/stream` | SSE streaming RAG query |
| POST | `/query/dependencies` | Dependency tree |
| GET | `/query/stats` | Knowledge base stats |
| POST | `/query/eval` | Retrieval evaluation (fixture-based, no LLM calls) |

### Modules
| Method | Path | Description |
|---|---|---|
| GET | `/modules/` | List modules with filters (repo, tag, resource_type, version) |
| GET | `/modules/versions/all` | All available versions |
| GET | `/modules/tags/all` | All tags with counts |
| GET | `/modules/{repo}/{path}/versions` | Module version history |
| GET | `/modules/{repo}/{path}/dependencies` | Module dependency tree |
| GET | `/modules/{repo}/{path}/dependents` | Who depends on this module |

### Consumer (knowledge layer)
| Method | Path | Description |
|---|---|---|
| POST | `/consumer/` | Index consumer repo (202 Accepted, Celery task) |
| GET | `/consumer/` | List consumer index jobs |
| GET | `/consumer/{job_id}` | Job status |
| POST | `/consumer/{job_id}/reindex` | Re-run with previous parameters |
| DELETE | `/consumer/{job_id}` | Delete job and cascading knowledge cleanup |
| POST | `/consumer/distill` | Manually trigger convention distillation |

### Snippets (knowledge browser)
| Method | Path | Description |
|---|---|---|
| GET | `/snippets/module-refs` | List modules with snippet counts |
| GET | `/snippets/consumer-repos` | List consumer repos |
| GET | `/snippets/module-refs/{module_ref}` | Conventions + usage examples for a module |

### Audit
| Method | Path | Description |
|---|---|---|
| GET | `/audit/` | Browse logs with filters (category, action, status, limit, offset) |
| GET | `/audit/stats` | Summary counts by category/status |

### Webhooks
| Method | Path | Description |
|---|---|---|
| POST | `/webhook/github` | Push/tag events (HMAC-SHA256 verify) |
| POST | `/webhook/gitlab` | Push events (token verify) |

### Health
| Method | Path | Description |
|---|---|---|
| GET | `/health` | `{status, version}` |

---

## Databases

### PostgreSQL / pgvector
- Table `modules` — main table with `vector(768)` embedding
- Table `index_jobs` — indexing history (status: pending/running/done/failed)
- Table `query_log` — optional query logging
- Table `knowledge_snippets` — usage observations and convention snippets (see Knowledge Layer)
- Table `consumer_index_jobs` — consumer repo indexing history with stats
- Table `audit_logs` — audit trail (category, action, status, duration, request/response, metadata)
- Table `schema_migrations` — tracks applied SQL migrations
- IVFFlat index with `lists=100` — for large databases (>10k modules) increase to 200-500
- Similarity search uses cosine distance (`<=>`)
- Unique constraint on `(repo, module_path, version)`
- Initial schema in `init_db.sql`; all subsequent changes via `migrations/`
- Table `module_dependencies` — dependency edges between modules (parent→child),
  queried via recursive CTEs for tree traversal and reverse-dependent lookup

---

## Known pitfalls

**pytorch + CUDA** — `sentence-transformers` pulls PyTorch with CUDA by default
(~4 GB). In the `Dockerfile` before `pip install -r requirements.txt` add:
```dockerfile
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu
```

**HCL parsing** — `python-hcl2` throws exceptions on invalid HCL (e.g. old
modules from Terraform 0.11). The parser already catches these errors and logs
a warning instead of interrupting indexing.

**GitHub webhook** — requires a public URL (or ngrok in dev). Alternative:
GitHub Actions workflow in `.github/workflows/rag-index.yml` works without
exposing an endpoint.

**`tags` in HCL** — the parser looks for tags in: `tags.txt`, `.tags`,
`locals.tf` (`tags` key in `locals` block), path segments, and auto-tags from
resource types. It does not look in `variables.tf` or `main.tf`.
If a repo has a different tagging convention, extend `_extract_tags()` in
`parser.py`.

**Nested modules** — `_find_module_dirs()` treats every directory with `.tf`
files as a separate module (skips `.terraform/`). For repos with very deep
nesting this may produce duplicates — filter by path prefix.

**Docker secrets (SSH)** — The worker uses Docker secrets for the SSH deploy key.
Key path: `${SSH_KEY_PATH:-./worker_deploy_key}`. The worker sets
`GIT_SSH_COMMAND` with `-o StrictHostKeyChecking=accept-new`.

**Celery + async** — `make_session_factory()` in `vector_store.py` creates a
separate SQLAlchemy engine per Celery task, because Celery runs in a different
event loop than FastAPI. Do not use `AsyncSessionLocal` in Celery tasks.

**Agent + local models** — Qwen3-14B with reasoning enabled puts chain-of-thought
in `reasoning_content` (displayed in collapsible UI panel) and the actual answer
in `content`. The agent strips `<think>` tags from content but never uses
`reasoning_content` as the answer. Reasoning quality and tool-use reliability
improve significantly with larger models (32B+) or Claude Sonnet.

**docker compose restart vs up -d** — `restart` does NOT reload `.env` variables.
Use `docker compose up -d --force-recreate <service>` to pick up env changes.

---

## Running locally

```bash
cp .env.example .env   # fill in passwords and API keys

# SSH deploy key (needed for cloning private repositories)
# Place as ./worker_deploy_key or set SSH_KEY_PATH in .env

docker compose up -d

# API logs
docker compose logs -f api

# Worker logs (indexing)
docker compose logs -f worker

# UI
open http://localhost:3000

# API docs (Swagger)
open http://localhost:8000/docs

```

### Frontend development (without Docker)

```bash
cd frontend
npm install
npm run dev    # Vite dev server :3000 with proxy to API :8000
```

## First indexing

```bash
curl -X POST http://localhost:8000/index/ \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "git@github.com:org/tf-modules.git", "branch": "main"}'
```

Progress visible in the **Index Jobs** tab in the UI or:

```bash
curl http://localhost:8000/index/{job_id}
```

---

## TODO / not implemented

- [ ] Re-indexing only changed files (git diff instead of full clone)
- [ ] Export tag migration plan as PR (GitHub API)
- [ ] `tfstate` support — indexing actual infrastructure state
- [ ] Rate limiting on query endpoints
- [ ] Unit tests for parser and retriever
- [ ] Streaming tool-use for Anthropic backend (currently non-streaming per turn)
