<p align="center">
  <strong style="font-size:2em">&#x2B21;</strong>
</p>

<h1 align="center">Terraform RAG</h1>

<p align="center">
  AI-powered knowledge base for your Terraform modules.<br>
  Index, search, compose, and audit - all from one place.
</p>

<p align="center">
  <a href="https://terraform-rag.io">Live Demo</a> -
  <a href="docs/ARCHITECTURE.md">Architecture</a> -
  <a href="#quick-start">Quick Start</a> -
  <a href="#mcp-server">MCP Server</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12-blue" alt="Python 3.12">
  <img src="https://img.shields.io/badge/FastAPI-0.111-009688" alt="FastAPI">
  <img src="https://img.shields.io/badge/pgvector-cosine-purple" alt="pgvector">
  <img src="https://img.shields.io/badge/MCP-Streamable_HTTP-green" alt="MCP">
  <img src="https://img.shields.io/badge/license-BSL_1.1-orange" alt="License">
</p>

---

## Try the live demo

**Web UI** - browse 2500+ modules across 150+ repos (AWS / Azure / GCP):

> https://terraform-rag.io
>
> Login: `demo@terraform-rag.io` / `demo` (read-only)

**MCP** - connect your IDE to the knowledge base:

```json
{
  "mcpServers": {
    "terraform-rag": {
      "type": "http",
      "url": "https://terraform-rag.io/mcp",
      "headers": {
        "Authorization": "Bearer trag_6c495d7a027369cb5d324d877626c272"
      }
    }
  }
}
```

8 tools available: `list_modules`, `get_module_details`, `get_dependencies`,
`get_module_usage`, `find_similar_usages`, `fetch_example_code`, `get_stats`,
`list_modules` (with `semantic_query` for natural language search).

---

## What it does

Point it at your Terraform module repositories. It clones them, parses every
HCL file, generates embeddings, and builds a searchable knowledge base in
PostgreSQL + pgvector. Then it learns _how_ those modules are actually used
across your consumer repos - naming patterns, variable conventions, tagging
strategies, deployment layouts - and distils that into authoritative guidance.

An agentic pipeline (Claude, Bedrock, or any OpenAI-compatible model)
autonomously explores the knowledge base before answering your questions.
Query from the web UI, the REST API, or directly from your IDE via MCP.

## See it in action

<table>
  <tr>
    <td align="center">
      <a href="https://terraform-rag.io">
        <img src="docs/screenshots/01-modules.png" width="400" alt="Module browser">
      </a>
      <br><sub><b>Module Browser</b> - 704 modules across 57 repos</sub>
    </td>
    <td align="center">
      <a href="https://terraform-rag.io">
        <img src="docs/screenshots/02-query-compose.png" width="400" alt="Agentic query">
      </a>
      <br><sub><b>Agentic Compose</b> - tool calls and reasoning in real time</sub>
    </td>
  </tr>
  <tr>
    <td align="center">
      <a href="https://terraform-rag.io">
        <img src="docs/screenshots/03-query-result.png" width="400" alt="Generated HCL">
      </a>
      <br><sub><b>Generated HCL</b> - syntax-highlighted output</sub>
    </td>
    <td align="center">
      <a href="https://terraform-rag.io">
        <img src="docs/screenshots/04-knowledge.png" width="400" alt="Knowledge browser">
      </a>
      <br><sub><b>Knowledge Browser</b> - conventions and usage patterns</sub>
    </td>
  </tr>
  <tr>
    <td align="center">
      <a href="https://terraform-rag.io">
        <img src="docs/screenshots/05-index-jobs.png" width="400" alt="Index jobs">
      </a>
      <br><sub><b>Index Jobs</b> - repository indexing dashboard</sub>
    </td>
    <td align="center">
      <a href="https://terraform-rag.io">
        <img src="docs/screenshots/06-audit-logs.png" width="400" alt="Audit logs">
      </a>
      <br><sub><b>Audit Logs</b> - full trail of LLM, MCP, and API calls</sub>
    </td>
  </tr>
</table>

## Features

**Agentic Query Pipeline** - not a simple RAG lookup. The LLM autonomously
browses modules, checks details, reads conventions, and fetches example code
across multiple turns before composing an answer. Four query modes: compose,
search, optimize, and audit.

**Knowledge Layer** - indexes consumer repos to learn real-world usage patterns.
Distils conventions across six dimensions (naming, variables, tagging, layout,
versions, deployment) and treats them as authoritative guidance in all prompts.

**MCP Server** - Streamable HTTP endpoint works with Claude Code, Cursor,
Windsurf, and any MCP-compatible client. Query your module knowledge base
directly from your IDE.

**Dependency Graph** - PostgreSQL recursive CTEs map the full dependency tree
between modules. Find what depends on what, trace impact, and visualize
relationships with a D3 force-directed graph.

**Version Tracking** - automatic git tag discovery with per-module version
history. Code-hash caching avoids redundant LLM/embedding calls on re-index.

**Flexible LLM Backend** - Anthropic (direct or Bedrock), OpenRouter, Ollama,
or any OpenAI-compatible endpoint. Swap models without changing code.

**CI/CD Integration** - GitHub Actions workflow and webhook endpoints for
automatic re-indexing when `.tf` files change.

**Authentication** - disabled (default), local email/password with JWT, or
ALB-terminated SSO via AWS Identity Center / OIDC.

## Quick Start

```bash
# 1. Clone and configure
git clone https://github.com/krzysztofgawrys/rag-for-terraform.git
cd rag-for-terraform
cp .env.example .env
# Edit .env - set POSTGRES_PASSWORD, JWT_SECRET, and your LLM API key

# 2. Start everything
docker compose up -d

# 3. Index your first repo
curl -X POST http://localhost:8000/index/ \
  -H "Content-Type: application/json" \
  -d '{"repo_url": "git@github.com:org/tf-modules.git", "branch": "main"}'
```

| Service | URL |
|---|---|
| Frontend | http://localhost:3000 |
| API docs | http://localhost:8000/docs |
| MCP endpoint | http://localhost:8000/mcp/ |

For private repos, place your SSH deploy key at `./worker_deploy_key`
(or set `SSH_KEY_PATH` in `.env`).

## MCP Server

Connect any MCP-compatible client to `http://localhost:8000/mcp/`.

**Claude Code** (`.mcp.json`):
```json
{
  "mcpServers": {
    "terraform-rag": {
      "type": "http",
      "url": "http://localhost:8000/mcp/"
    }
  }
}
```

Available tools: `query_modules`, `pick_modules`, `list_modules`,
`get_module_details`, `get_dependencies`, `get_module_usage`,
`find_similar_usages`, `fetch_example_code`, `get_stats`.

## LLM Configuration

| Mode | `LLM_BASE_URL` | `LLM_MODEL` example |
|---|---|---|
| Anthropic (direct) | _(empty)_ | `claude-sonnet-4-6` |
| AWS Bedrock | _(set `AWS_BEDROCK_REGION`)_ | `us.anthropic.claude-sonnet-4-6-20250514-v1:0` |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4-6` |
| Ollama (local) | `http://ollama:11434/v1` | `qwen2.5-coder:32b` |

A separate cheap model can be used for module descriptions during indexing
(`DESCRIPTION_LLM_*` variables).

## Architecture

```
  Browser         AI Agent / IDE
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

For the full technical deep-dive - stack details, directory structure, API
endpoints, agent internals, knowledge layer pipeline, known limitations, and
deployment notes - see **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

## License

Business Source License 1.1 - see [LICENSE](LICENSE) for details.

- Non-production use (evaluation, testing, development) is permitted
- Production use requires a commercial license from the author
- On 2029-05-25 the license converts to AGPL-3.0
