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
| Query | `POST /query/`, `POST /query/stream` (SSE), `POST /query/dependencies`, `GET /query/stats` |
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

## License

Business Source License 1.1 -- see [LICENSE](LICENSE) for details.

- Non-production use (evaluation, testing, development) is permitted
- Production use requires a commercial license from the author
- On 2029-05-25 the license converts to AGPL-3.0
