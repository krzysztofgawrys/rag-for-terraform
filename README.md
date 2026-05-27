# Terraform RAG

RAG (Retrieval-Augmented Generation) backend for organisational Terraform module
knowledge. Index your module repositories, learn how they're used across consumer
repos, distil conventions, and query everything through a chat UI, REST API, or
MCP tools from any AI agent or IDE.

## Features

- **Module indexing** -- clone repos, parse HCL, generate embeddings, store in
  pgvector. Code-hash caching avoids redundant LLM/embedding calls on re-index.
- **Agentic query pipeline** -- iterative tool-use loop where the LLM
  autonomously browses modules, checks details, reads conventions, and fetches
  example code before composing an answer.
- **Knowledge layer** -- index consumer repos to learn _how_ modules are
  actually used; distil conventions (naming, variables, tagging, layout,
  versions, codeploy) and inject them into RAG prompts as authoritative
  guidance.
- **Dependency analysis** -- PostgreSQL recursive CTEs for dependency trees and
  reverse-dependent lookups.
- **Version tracking** -- git tag discovery, per-module version history.
- **MCP server** -- Streamable HTTP endpoint compatible with any MCP client
  (Claude Code, Cursor, Windsurf, custom agents, etc.).
- **CI/CD integration** -- GitHub/GitLab webhooks and a GitHub Actions workflow
  for automatic re-indexing on `.tf` changes.
- **Authentication** -- disabled (default), local email/password + JWT, or
  ALB-terminated SSO (AWS Identity Center / OIDC).
- **Audit logging** -- non-blocking event logging to PostgreSQL (API, MCP, LLM,
  worker calls).
- **Retrieval evaluation** -- YAML fixture with queries and expected module
  references; CLI and API harness measure hit rate and recall on your own data.

## Architecture

```
  Browser         AI Agent
     |               |
+---------+      +--------+      +----------------+
| Frontend|----->|  API   |----->| PostgreSQL 16  |
| (Vite)  |      | FastAPI|      | + pgvector     |
+---------+      +---+----+      +----------------+
                     |
                +----+----+
                | Worker  |      +-------+
                | (Celery)|----->| Redis |
                +---------+      +-------+
```

| Layer | Technology |
|---|---|
| API | FastAPI + uvicorn (async) |
| Vector DB | PostgreSQL 16 + pgvector (cosine similarity) |
| Task queue | Celery + Redis |
| Embeddings | sentence-transformers (CPU) or Amazon Bedrock Titan |
| LLM | Anthropic SDK, AWS Bedrock, or OpenAI-compatible (Ollama, OpenRouter) |
| HCL parser | python-hcl2 |
| MCP | FastMCP, mounted as Streamable HTTP |
| Frontend | TypeScript + Vite, D3, highlight.js, marked |

## Quick start

### Prerequisites

- Docker & Docker Compose
- An SSH deploy key if you need to clone private repositories
- An LLM API key (Anthropic, OpenRouter, AWS Bedrock, or a local Ollama instance)

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env - set at minimum:
#   POSTGRES_PASSWORD
#   ANTHROPIC_API_KEY  (or configure Bedrock / Ollama / OpenRouter)
#   JWT_SECRET
```

### 2. (Optional) SSH deploy key

If your Terraform repos are private, place the deploy key at `./worker_deploy_key`
(or set `SSH_KEY_PATH` in `.env`).

### 3. Start services

```bash
docker compose up -d
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API (Swagger) | http://localhost:8000/docs |
| API health | http://localhost:8000/health |

### 4. Index your first repository

```bash
curl -X POST http://localhost:8000/index/ \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "git@github.com:org/tf-modules.git", "branch": "main"}'
```

Track progress in the **Index Jobs** tab or:

```bash
curl http://localhost:8000/index/{job_id}
```

### 5. Measure retrieval accuracy

Once indexing is complete, run the evaluation harness against your modules to
get a baseline retrieval score:

```bash
python scripts/eval_retrieval.py --url http://localhost:8000
# or via the API directly:
curl -X POST http://localhost:8000/query/eval
```

Edit `scripts/eval_queries.yaml` to add 20-30 queries with expected module
references for your organisation. See [Retrieval evaluation](#retrieval-evaluation)
for details.

## LLM configuration

The LLM backend is selected by `LLM_BASE_URL` and `AWS_BEDROCK_REGION`:

| Mode | Configuration | `LLM_MODEL` example |
|---|---|---|
| Anthropic (direct) | `LLM_BASE_URL=` | `claude-sonnet-4-6` |
| AWS Bedrock | `AWS_BEDROCK_REGION=us-east-1` | `us.anthropic.claude-sonnet-4-6-20250514-v1:0` |
| OpenRouter | `LLM_BASE_URL=https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4-6` |
| Ollama (local) | `LLM_BASE_URL=http://ollama:11434/v1` | `qwen2.5-coder:32b` |

A separate model can be configured for module descriptions during indexing
(`DESCRIPTION_LLM_*` variables) to keep costs down.

## Agentic mode

Set `AGENT_COMPOSE_ENABLED=true` to replace the single-shot retrieval pipeline
with an iterative tool-use agent. The agent autonomously explores the knowledge
base using six tools (`list_modules`, `get_module_details`, `get_dependencies`,
`get_module_usage`, `find_similar_usages`, `fetch_example_code`) before composing
its answer. Works with both Anthropic and OpenAI-compatible backends.

Supports four query types:

| Type | Purpose |
|---|---|
| **compose** | Generate HCL code for a component or full stack |
| **optimize** | Review code for version drift, convention violations, DRY |
| **audit** | Security & compliance review (IAM, encryption, networking) |
| **search** | Q&A, module comparison, usage guidance |

## Knowledge layer

Beyond the core module index, the system learns how modules are used in practice:

1. **Consumer indexing** -- parse `module {}` blocks from consumer repos, build
   usage summaries, embed and store them.
2. **Convention distillation** -- an LLM distils usage summaries into convention
   snippets across six dimensions (naming, vars, codeploy, tagging, layout,
   versions) with quality self-evaluation.
3. **Stack patterns** -- detect recurring multi-module compositions across repos.

Conventions are treated as **authoritative** in all RAG prompts -- they override
generic Terraform best practices.

## MCP server

The API exposes an MCP server at `/mcp/` (Streamable HTTP). Any MCP-compatible
client can connect to it - Claude Code, Cursor, Windsurf, custom agents, etc.

**Claude Code** (`.mcp.json`):

```jsonc
{
  "mcpServers": {
    "terraform-rag": {
      "type": "http",
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

**Cursor / Windsurf** - add via settings UI or config file, pointing to the
same `http://localhost:8000/mcp/` URL.

**Custom agents** - use any MCP SDK (`@modelcontextprotocol/sdk`,
`mcp` Python package, etc.) to connect via Streamable HTTP transport.

Available tools: `query_modules`, `pick_modules`, `list_modules`,
`get_module_details`, `get_dependencies`, `get_module_usage`,
`find_similar_usages`, `fetch_example_code`, `get_stats`.

**Recommended workflow for code generation:**

1. `pick_modules(query)` -- get a list of relevant module references
2. `get_module_details(repo, module_path)` -- full variables, outputs, conventions
3. Write HCL yourself using those details

## CI/CD integration

### GitHub Actions

Copy `.github/workflows/rag-index.yml` into your Terraform module repos. On
pushes to `main` that change `.tf` files, it triggers re-indexing automatically.

Required secrets: `RAG_BACKEND_URL`, `RAG_API_KEY`.

### Webhooks

| Endpoint | Verification |
|---|---|
| `POST /webhook/github` | HMAC-SHA256 (`GITHUB_WEBHOOK_SECRET`) |
| `POST /webhook/gitlab` | Token header (`GITLAB_WEBHOOK_TOKEN`) |

## Frontend development

```bash
cd frontend
npm install
npm run dev    # Vite dev server at :3000, proxies API requests to :8000
```

## API overview

| Group | Endpoints |
|---|---|
| Index | `POST /index/`, `GET /index/`, `GET /index/{id}`, `DELETE /index/{id}`, `POST /index/{id}/reindex` |
| Query | `POST /query/`, `POST /query/stream` (SSE), `POST /query/dependencies`, `GET /query/stats`, `POST /query/eval` |
| Modules | `GET /modules/`, `/modules/versions/all`, `/modules/repos/all`, `/modules/tags/all`, per-module versions/deps/dependents |
| Consumer | `POST /consumer/`, `GET /consumer/`, `GET /consumer/{id}`, `DELETE /consumer/{id}`, `POST /consumer/{id}/reindex`, `POST /consumer/distill` |
| Knowledge | `GET /snippets/module-refs`, `/snippets/consumer-repos`, `/snippets/module-refs/{ref}` |
| Audit | `GET /audit/`, `GET /audit/stats` |
| Auth | `GET /auth/info`, `POST /auth/login`, `POST /auth/refresh`, `GET /auth/me`, `POST /auth/logout`, API key management |
| Health | `GET /health` |

Full Swagger documentation is available at `/docs` when the API is running.

## Production deployment

A production Docker Compose file (`docker-compose.prod.yml`) is included. It
differs from the dev setup by not exposing database/Redis ports to the host
and not using bind-mount volumes.

## Project structure

```
app/
  main.py                 # FastAPI app, MCP mount, lifespan
  core/                   # Config, parser, embeddings, vector store, LLM, audit
  services/               # Indexer, retriever, agent, consumer indexer, distiller
  api/
    mcp_tools.py          # MCP server and tool definitions
    routes/               # REST API routes
  workers/                # Celery task definitions
  models/                 # Pydantic request/response schemas
frontend/                 # TypeScript SPA (Vite + D3)
migrations/               # Numbered SQL migration files
scripts/                  # DB init schema, worker entrypoint, utility scripts
.github/workflows/        # CI/CD automation
```

## Known limitations and open questions

### Convention authority vs correctness

Distilled conventions are treated as **authoritative** in RAG prompts - they
override generic Terraform best practices. This works well when the
organisation's usage is healthy, but creates a failure mode: if 50 repos use a
module in an outdated or insecure way, the distiller will faithfully capture
that pattern, the self-evaluation will score it highly (it _is_ consistent with
the data), and the agent will recommend it with confidence.

The self-evaluation step ([eval.md](app/prompts/distiller/eval.md)) checks
**faithfulness to the source usages** - whether the convention accurately
reflects what the data says - not whether the pattern itself is correct,
secure, or up to date. Score 5 means "every claim is evidenced", not "this is
a good idea". Frequency is not correctness, and this layer does not distinguish
between the two.

Mitigations that exist today: confidence labels (STRONG/MODERATE/WEAK/
LOW\_EVIDENCE based on evidence count), `stale` flag for conventions scoring
below 3, and the `eval_reason` field for manual review. None of these catch a
_consistently wrong_ pattern. A proper fix would require an external
correctness signal - e.g. security policy rules, version-freshness checks, or
human review of high-evidence conventions.

### Manual convention distillation

Module re-indexing triggers automatically via webhooks and GitHub Actions, but
convention distillation does not. It requires a manual `POST /consumer/distill`
call. This means the knowledge layer can drift from the actual module state
until someone remembers to run it.

This is a conscious trade-off: distillation is expensive (one LLM call per
module-ref per dimension), and auto-triggering on every consumer re-index would
cause unnecessary churn when modules haven't changed. The cost is that
conventions may lag behind reality. If this matters for your deployment,
consider adding distillation to your CI pipeline or scheduling it as a cron
job.

### Vector space scaling

Every module version gets its own embedding row (`UNIQUE (repo, module_path,
version)`). Code-hash caching avoids redundant LLM/embedding calls when the
code hasn't changed, but it does not reduce the number of stored vectors -
each version still occupies a row in the index.

For repositories with many tagged versions, this means the vector count grows
with `modules x versions`, not just `modules`. There is currently **no
automatic pruning** of old versions from the vector index.

The pgvector indexes are IVFFlat with `lists=100` (modules) and `lists=200`
(knowledge\_snippets). This is adequate for up to roughly 10k vectors; beyond
that, `lists` should be increased (general guideline: `sqrt(row_count)`) or
the index type switched to HNSW for better recall at scale. Neither scaling
strategy is automated.

### Retrieval evaluation

A retrieval evaluation harness is included (`scripts/eval_retrieval.py`). It
runs a set of queries from a YAML fixture (`scripts/eval_queries.yaml`) against
the live index and measures whether the expected modules appear in the top-K
results.

```bash
# CLI (human report)
python scripts/eval_retrieval.py --url http://localhost:8000

# JSON output (CI-friendly)
python scripts/eval_retrieval.py --json

# API endpoint (retrieval only, no LLM calls)
curl -X POST http://localhost:8000/query/eval
```

The fixture ships with example entries - replace them with your org's real
modules and queries (20-30 cases recommended). Each entry specifies:
- a natural-language query
- the `module_ref`s that should appear in sources (`repo/module_path`)
- match mode (`any` = at least one found, `all` = every ref found)

The harness reports two numbers: **query hit rate** (how many queries found at
least one expected module) and **module recall** (what fraction of all expected
refs were returned). These serve double duty: catching retrieval regressions
and providing "X% accuracy on your modules" for POC pitches.

**What it does not cover**: answer quality, convention correctness, or
end-to-end generation accuracy. Those require human evaluation or a separate
LLM-as-judge layer - neither is implemented yet.

## License

Business Source License 1.1 -- see [LICENSE](LICENSE) for details.

- Non-production use (evaluation, testing, development) is permitted
- Production use requires a commercial license from the author
- On 2029-05-25 the license converts to AGPL-3.0
