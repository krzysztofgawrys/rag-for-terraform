"""
Agent-based compose/generate pipeline.

Replaces the one-shot shopping-list approach with an iterative LLM agent
that has direct access to the knowledge-base tools (same as MCP). The agent
autonomously browses modules, checks details, reads conventions, and then
composes the final HCL.

Backend selection (same convention as app/core/llm.py):
  - settings.llm_base_url == ""   → Anthropic API (tool_use blocks)
  - settings.llm_base_url != ""   → OpenAI-compatible (tool_calls, e.g. LMStudio/Ollama/OpenRouter)

Falls back to the existing pipeline (in retriever.py) when:
  - AGENT_COMPOSE_ENABLED=false, or
  - no ANTHROPIC_API_KEY (the value is always required as the API key field
    even when LLM_BASE_URL points to a local model)
"""
from __future__ import annotations

import json as _json
import re
import time
from typing import AsyncGenerator

import structlog

from app.core.config import get_settings
from app.core.audit import emit
from app.core.embeddings import embed_query
from app.core import vector_store as vs
from app.prompts import load_prompt
from app.models.schemas import QueryRequest, QueryResponse, QueryResult

log = structlog.get_logger()

# ---------------------------------------------------------------------------
# Tool definitions (Anthropic native format — converted on-the-fly for OpenAI)
# ---------------------------------------------------------------------------
# Subset of MCP tools — excludes query_modules (the pipeline we replace) and
# pick_modules (the shopping list we replace). The agent discovers modules
# through list_modules + find_similar_usages instead.

AGENT_TOOLS = [
    {
        "name": "list_modules",
        "description": (
            "List indexed Terraform modules (one entry per unique module, latest version). "
            "Two modes: use `search` for substring name/path match, or `semantic_query` for "
            "natural language similarity search (e.g. 'ECS Fargate service with ALB'). "
            "Semantic mode ranks results by relevance. Filters (repo, tag) apply in both modes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Filter by repository name (e.g. my-terraform-modules)."},
                "tag": {"type": "string", "description": "Filter by tag (e.g. s3, networking, prod)."},
                "resource_type": {"type": "string", "description": "Filter by AWS resource type (e.g. aws_s3_bucket)."},
                "search": {"type": "string", "description": "Case-insensitive substring match on module name or path."},
                "semantic_query": {"type": "string", "description": "Natural language query for semantic similarity search (e.g. 'VPC with private subnets')."},
                "limit": {"type": "integer", "description": "Max results (default 30).", "default": 30},
            },
            "required": [],
        },
    },
    {
        "name": "get_module_details",
        "description": (
            "Get full details for a specific module: description, all versions, "
            "variables (with types, defaults, descriptions), outputs, and resources. "
            "Call this for every module you plan to include in the final HCL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository name (e.g. my-terraform-modules)."},
                "module_path": {"type": "string", "description": "Path relative to repo root (e.g. modules/s3/basic)."},
            },
            "required": ["repo", "module_path"],
        },
    },
    {
        "name": "get_dependencies",
        "description": (
            "Get the dependency tree and reverse dependents for a module. "
            "Shows what the module depends on and which other modules use it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository name."},
                "module_path": {"type": "string", "description": "Module path relative to repo root."},
                "depth": {"type": "integer", "description": "Dependency tree depth (default 3).", "default": 3},
            },
            "required": ["repo", "module_path"],
        },
    },
    {
        "name": "get_module_usage",
        "description": (
            "Get usage conventions and examples for a module. Returns convention "
            "snippets (naming, vars, codeploy, tagging, layout, versions) and "
            "recent usage observations from consumer repos. Use to understand HOW "
            "a module is typically used before composing HCL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module_ref": {
                    "type": "string",
                    "description": "Module reference in format repo/module_path (e.g. my-modules-repo/vpc).",
                },
                "top_k": {"type": "integer", "description": "Max usage examples (default 10).", "default": 10},
            },
            "required": ["module_ref"],
        },
    },
    {
        "name": "find_similar_usages",
        "description": (
            "Semantic search across all usage and convention knowledge snippets. "
            "Use to find how modules are used in practice, discover relevant "
            "modules by natural language query, or find convention patterns."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural language query (e.g. 'VPC setup for production')."},
                "top_k": {"type": "integer", "description": "Max results (default 10).", "default": 10},
                "module_ref": {"type": "string", "description": "Optional filter to a specific module."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "fetch_example_code",
        "description": (
            "Fetch a raw HCL code fragment from git by source_locator. "
            "Returns actual Terraform code from a real deployment. "
            "source_locator format: 'repo@sha:path/file.tf:L1-L45'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_locator": {
                    "type": "string",
                    "description": "Locator string (e.g. 'tf-infra-prod@abc123:eu-west-1/main.tf:L1-L45').",
                },
            },
            "required": ["source_locator"],
        },
    },
]

# Maximum chars per tool result stored in message history.
_MAX_TOOL_RESULT_CHARS = 6000


def _tools_for_openai() -> list[dict]:
    """Convert Anthropic tool format to OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in AGENT_TOOLS
    ]


# ---------------------------------------------------------------------------
# Tool executor (shared between backends)
# ---------------------------------------------------------------------------

def _sanitize_tool_input(tool_input: dict) -> dict:
    """Fix malformed tool arguments from models.

    Handles:
    - <parameter=...> tags injected into values
    - double slashes in paths/refs (repo//path → repo/path)
    """
    cleaned = {}
    for k, v in tool_input.items():
        if isinstance(v, str):
            if '<parameter=' in v:
                v = v.split('<parameter=')[0].strip().strip('/')
            while '//' in v:
                v = v.replace('//', '/')
        cleaned[k] = v
    return cleaned


async def _resolve_module_ref(tool_input: dict) -> dict:
    """Fix module_ref missing the repo prefix (e.g. 'networking/vpc' → 'repo/networking/vpc').

    Also handles repo+module_path passed together in repo field
    (e.g. repo='my-modules/networking/vpc', module_path='').
    """
    # Fix module_ref without repo prefix
    if "module_ref" in tool_input:
        ref = tool_input["module_ref"]
        # Try DB lookup: find a module whose module_path matches the ref
        if ref and "/" in ref:
            parts = ref.split("/", 1)
            # Check if first segment is already a repo by seeing if it looks like 'terraform-*'
            if not parts[0].startswith("terraform-"):
                # Likely just a module_path — search for it
                from app.core.vector_store import AsyncSessionLocal
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import text
                    row = await db.execute(
                        text("SELECT DISTINCT repo FROM modules WHERE module_path = :path LIMIT 1"),
                        {"path": ref},
                    )
                    found = row.scalar_one_or_none()
                    if found:
                        tool_input["module_ref"] = f"{found}/{ref}"

    # Fix source_locator with module_path instead of repo name
    if "source_locator" in tool_input:
        loc = tool_input["source_locator"]
        if "@" in loc:
            repo_part = loc.split("@", 1)[0]
            if not repo_part.startswith("terraform-"):
                from app.core.vector_store import AsyncSessionLocal
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import text
                    row = await db.execute(
                        text("SELECT DISTINCT repo FROM modules WHERE module_path = :path LIMIT 1"),
                        {"path": repo_part},
                    )
                    found = row.scalar_one_or_none()
                    if found:
                        tool_input["source_locator"] = found + "@" + loc.split("@", 1)[1]

    # Fix repo containing full path (repo='terraform-x/managed/artemis', module_path='')
    if "repo" in tool_input and "module_path" in tool_input:
        repo = tool_input["repo"]
        mp = tool_input["module_path"]
        if not mp and "/" in repo and repo.startswith("terraform-"):
            # Split on first slash after the repo name pattern
            parts = repo.split("/", 1)
            if len(parts) == 2:
                from app.core.vector_store import AsyncSessionLocal
                async with AsyncSessionLocal() as db:
                    from sqlalchemy import text
                    row = await db.execute(
                        text("SELECT 1 FROM modules WHERE repo = :repo AND module_path = :path LIMIT 1"),
                        {"repo": parts[0], "path": parts[1]},
                    )
                    if row.scalar_one_or_none():
                        tool_input["repo"] = parts[0]
                        tool_input["module_path"] = parts[1]

    return tool_input


async def _execute_tool(name: str, tool_input: dict) -> str:
    """Dispatch to the actual MCP tool function. Returns the tool output string."""
    from app.api import mcp_tools

    tool_input = _sanitize_tool_input(tool_input)
    tool_input = await _resolve_module_ref(tool_input)

    dispatch = {
        "list_modules": mcp_tools.list_modules,
        "get_module_details": mcp_tools.get_module_details,
        "get_dependencies": mcp_tools.get_dependencies,
        "get_module_usage": mcp_tools.get_module_usage,
        "find_similar_usages": mcp_tools.find_similar_usages,
        "fetch_example_code": mcp_tools.fetch_example_code,
    }

    fn = dispatch.get(name)
    if not fn:
        return f"Unknown tool: {name}"
    try:
        result = await fn(**tool_input)
        return result
    except Exception as exc:
        log.warning("agent_tool_error", tool=name, error=str(exc)[:200])
        return f"Tool error ({name}): {exc}"


def _truncate_tool_result(result_text: str) -> str:
    if len(result_text) <= _MAX_TOOL_RESULT_CHARS:
        return result_text
    return result_text[:_MAX_TOOL_RESULT_CHARS] + f"\n\n... (truncated, {len(result_text)} chars total)"


# ---------------------------------------------------------------------------
# Initial context builder
# ---------------------------------------------------------------------------

def _is_compose_mode(query_type: str) -> bool:
    return query_type in ("generate", "compose")


async def _build_initial_context(request: QueryRequest, db) -> tuple[str, list[dict]]:
    """Build seed context for the agent's first message.

    Compose: catalog + compose/stack patterns + semantic search (heavy).
    Others:  semantic search only (lightweight).
    """
    from app.services.retriever import _search_compose_patterns

    query_vec = embed_query(request.query, query_type=request.query_type)

    similar = await vs.similarity_search(
        db,
        query_embedding=query_vec,
        top_k=request.top_k,
        repo_filter=request.repo_filter,
        tag_filter=request.tag_filter,
        version_filter=request.version_filter,
    )

    parts: list[str] = []

    if similar:
        lines = ["## Initial semantic matches\n"
                 "(repo and module_path shown separately for tool calls)\n"]
        for r in similar[:8]:
            desc = (r.get("description") or "").split("\n", 1)[0][:120]
            sim = int(r.get("similarity", 0) * 100)
            lines.append(f"- repo=`{r['repo']}` path=`{r['module_path']}` (sim {sim}%) — {desc}")
        parts.append("\n".join(lines))

    # Compose-specific: add catalog + stack patterns
    if _is_compose_mode(request.query_type):
        from app.services.retriever import _build_module_catalog
        compose_ctx = await _search_compose_patterns(db, query_vec)
        if compose_ctx:
            parts.append(compose_ctx)

        catalog = await _build_module_catalog(db)
        if catalog:
            parts.append(catalog)

    context = "\n\n---\n\n".join(parts)
    return context, similar


# ---------------------------------------------------------------------------
# Agent system prompts (per query type) — loaded from app/prompts/agent/
# ---------------------------------------------------------------------------

_TOOL_PREAMBLE = load_prompt("agent/tool_preamble.md")

def _build_agent_system_prompt(query_type: str) -> str:
    """Build a system prompt by loading the .md file and injecting tool_preamble."""
    _type = query_type if query_type != "generate" else "compose"
    _type = _type if _type in ("compose", "optimize", "audit", "search") else "search"
    raw = load_prompt(f"agent/{_type}.md")
    return raw.replace("{tool_preamble}", _TOOL_PREAMBLE)


# Keep the dict for backwards compatibility (e.g. tests, imports)
AGENT_SYSTEM_PROMPTS: dict[str, str] = {
    t: _build_agent_system_prompt(t) for t in ("compose", "optimize", "audit", "search")
}
AGENT_SYSTEM_PROMPTS["generate"] = AGENT_SYSTEM_PROMPTS["compose"]


def _get_agent_system_prompt(query_type: str) -> str:
    return AGENT_SYSTEM_PROMPTS.get(query_type, AGENT_SYSTEM_PROMPTS["search"])


_USER_MESSAGE_TEMPLATES: dict[str, str] = {
    t: load_prompt(f"agent/user_{t}.md")
    for t in ("compose", "optimize", "audit", "search")
}
_USER_MESSAGE_TEMPLATES["generate"] = _USER_MESSAGE_TEMPLATES["compose"]


def _build_user_message(request: QueryRequest, initial_context: str) -> str:
    template = _USER_MESSAGE_TEMPLATES.get(
        request.query_type, _USER_MESSAGE_TEMPLATES["search"]
    )
    msg = template.format(query=request.query, context=initial_context)

    # Append active filters as constraints so the agent respects them in tool calls
    constraints: list[str] = []
    if request.repo_filter:
        repos = request.repo_filter if isinstance(request.repo_filter, list) else [request.repo_filter]
        constraints.append(f"repositories: {', '.join(repos)}")
    if request.tag_filter:
        tags = request.tag_filter if isinstance(request.tag_filter, list) else [request.tag_filter]
        constraints.append(f"tags: {', '.join(tags)}")
    if request.version_filter:
        versions = request.version_filter if isinstance(request.version_filter, list) else [request.version_filter]
        constraints.append(f"versions: {', '.join(versions)}")

    if constraints:
        msg += (
            "\n\n---\n\n"
            "**SCOPE CONSTRAINT — the user has applied filters. "
            "Limit your search and recommendations to:**\n"
            + "\n".join(f"- {c}" for c in constraints)
            + "\nDo NOT use modules outside this scope unless absolutely necessary "
            "(and flag it explicitly if you do)."
        )

    return msg


def _build_sources(initial_modules: list[dict]) -> list[dict]:
    return [
        {
            "module_name": r["module_name"],
            "repo": r["repo"],
            "module_path": r["module_path"],
            "version": r.get("version", ""),
            "tags": r["tags"] or [],
            "similarity": round(float(r["similarity"]), 4),
            "description": r["description"],
        }
        for r in initial_modules
    ]


def _backend() -> str:
    """Return 'openai' or 'anthropic' based on settings.llm_base_url."""
    return "openai" if get_settings().llm_base_url else "anthropic"


def _agent_model() -> str:
    settings = get_settings()
    return settings.agent_model or settings.llm_model


# ---------------------------------------------------------------------------
# Anthropic backend — one turn
# ---------------------------------------------------------------------------

async def _anthropic_turn(client, messages: list[dict], system_prompt: str) -> dict:
    """Execute one Anthropic agent turn. Returns a normalized response dict."""
    settings = get_settings()
    kwargs: dict = dict(
        model=_agent_model(),
        max_tokens=16384,
        system=[{
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        }],
        tools=AGENT_TOOLS,
        messages=messages,
    )
    # Extended thinking — only for Anthropic / Bedrock backends.
    # Requires temperature=1 and forbids top_k/top_p (defaults are fine).
    if settings.agent_thinking_budget > 0:
        kwargs["thinking"] = {
            "type": "enabled",
            "budget_tokens": settings.agent_thinking_budget,
        }
    response = await client.messages.create(**kwargs)

    text_blocks: list[str] = []
    tool_calls: list[dict] = []
    raw_content: list[dict] = []

    for block in response.content:
        if block.type == "thinking":
            # Required: extended thinking blocks must be preserved (with signature)
            # in the assistant message when the next turn includes tool_result.
            entry: dict = {"type": "thinking", "thinking": getattr(block, "thinking", "")}
            sig = getattr(block, "signature", None)
            if sig:
                entry["signature"] = sig
            raw_content.append(entry)
        elif block.type == "redacted_thinking":
            # Encrypted thinking the model produced but we cannot read — still
            # must be passed back verbatim.
            data = getattr(block, "data", None)
            if data:
                raw_content.append({"type": "redacted_thinking", "data": data})
        elif block.type == "text" and block.text:
            text_blocks.append(block.text)
            raw_content.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            tool_calls.append({"id": block.id, "name": block.name, "input": block.input})
            raw_content.append({
                "type": "tool_use",
                "id": block.id,
                "name": block.name,
                "input": block.input,
            })

    # Anthropic models with extended thinking may include thinking blocks
    reasoning_parts = [
        b.thinking for b in response.content
        if getattr(b, "type", None) == "thinking" and getattr(b, "thinking", None)
    ]

    return {
        "text_blocks": text_blocks,
        "tool_calls": tool_calls,
        "stop": response.stop_reason == "end_turn",
        "reasoning": "\n".join(reasoning_parts),
        "raw_content": raw_content,
    }


def _anthropic_append_assistant(messages: list[dict], raw_content: list[dict]) -> None:
    messages.append({"role": "assistant", "content": raw_content})


def _anthropic_append_tool_results(messages: list[dict], results: list[dict]) -> None:
    """results: list of {'tool_use_id', 'content'}"""
    messages.append({
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": r["tool_use_id"], "content": r["content"]}
            for r in results
        ],
    })


# ---------------------------------------------------------------------------
# OpenAI-compatible backend — one turn (streaming)
# ---------------------------------------------------------------------------

async def _openai_turn_stream(client, messages: list[dict], turn: int):
    """Streaming OpenAI-compatible agent turn.

    Yields SSE event strings for live reasoning/content, then yields
    a final result dict (same shape as _anthropic_turn return).
    Caller checks: if isinstance(event, str) → yield to client;
                   if isinstance(event, dict) → process as turn result.
    """
    stream = await client.chat.completions.create(
        model=_agent_model(),
        max_tokens=16384,
        messages=messages,
        tools=_tools_for_openai(),
        tool_choice="auto",
        stream=True,
    )

    reasoning_buf = ""
    content_buf = ""
    reasoning_started = False
    content_started = False
    finish_reason = None

    # Accumulate tool_call deltas (they arrive in pieces)
    tc_builders: dict[int, dict] = {}  # index → {id, name, arguments}

    async for chunk in stream:
        choice = chunk.choices[0] if chunk.choices else None
        if not choice:
            continue
        delta = choice.delta
        finish_reason = choice.finish_reason or finish_reason

        # --- reasoning_content deltas (live thinking) ---
        rc = getattr(delta, "reasoning_content", None)
        if rc:
            if not reasoning_started:
                reasoning_started = True
                yield f"data: {_json.dumps({'type': 'reasoning_start', 'turn': turn})}\n\n"
            reasoning_buf += rc
            yield f"data: {_json.dumps({'type': 'reasoning', 'token': rc, 'turn': turn})}\n\n"

        # --- content deltas (actual answer) ---
        if delta.content:
            if reasoning_started and not content_started:
                # Reasoning finished, content starting — close panel
                yield f"data: {_json.dumps({'type': 'reasoning_end', 'turn': turn})}\n\n"
                reasoning_started = False
            content_started = True
            content_buf += delta.content
            yield f"data: {_json.dumps({'type': 'token', 'token': delta.content})}\n\n"

        # --- tool_call deltas ---
        if delta.tool_calls:
            for tc_delta in delta.tool_calls:
                idx = tc_delta.index
                if idx not in tc_builders:
                    tc_builders[idx] = {
                        "id": tc_delta.id or "",
                        "name": (tc_delta.function.name if tc_delta.function else "") or "",
                        "arguments": "",
                    }
                else:
                    if tc_delta.id:
                        tc_builders[idx]["id"] = tc_delta.id
                    if tc_delta.function and tc_delta.function.name:
                        tc_builders[idx]["name"] = tc_delta.function.name
                if tc_delta.function and tc_delta.function.arguments:
                    tc_builders[idx]["arguments"] += tc_delta.function.arguments

    # Close reasoning panel if it was still open (e.g. tool_calls without content)
    if reasoning_started:
        yield f"data: {_json.dumps({'type': 'reasoning_end', 'turn': turn})}\n\n"

    # Build tool_calls list
    tool_calls: list[dict] = []
    raw_tool_calls: list[dict] = []
    for idx in sorted(tc_builders.keys()):
        b = tc_builders[idx]
        try:
            tc_input = _json.loads(b["arguments"] or "{}")
        except Exception:
            tc_input = {}
        tool_calls.append({"id": b["id"], "name": b["name"], "input": tc_input})
        raw_tool_calls.append({
            "id": b["id"],
            "type": "function",
            "function": {"name": b["name"], "arguments": b["arguments"] or "{}"},
        })

    content = _strip_think_tags(content_buf.strip())
    text_blocks = [content] if content else []
    stop = finish_reason in ("stop", "length") and not tool_calls

    # Yield final result dict
    yield {
        "text_blocks": text_blocks,
        "tool_calls": tool_calls,
        "stop": stop,
        "reasoning": reasoning_buf.strip(),
        "raw_content": {"content": content, "tool_calls": raw_tool_calls},
    }


def _openai_append_assistant(messages: list[dict], raw_content: dict) -> None:
    msg: dict = {"role": "assistant"}
    msg["content"] = raw_content.get("content") or None
    if raw_content.get("tool_calls"):
        msg["tool_calls"] = raw_content["tool_calls"]
    messages.append(msg)


def _openai_append_tool_results(messages: list[dict], results: list[dict]) -> None:
    for r in results:
        messages.append({
            "role": "tool",
            "tool_call_id": r["tool_use_id"],
            "content": r["content"],
        })


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks (Qwen3 reasoning chatter)."""
    import re
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Agent loop — streaming (backend-agnostic)
# ---------------------------------------------------------------------------

async def agent_stream_query(
    request: QueryRequest, db,
) -> AsyncGenerator[str, None]:
    """Run the agent loop and yield SSE events."""
    settings = get_settings()
    t0 = time.monotonic()
    backend = _backend()
    agent_model = _agent_model()
    max_turns = settings.agent_max_turns
    tool_call_count = 0

    # Phase 1: build initial context
    try:
        initial_context, initial_modules = await _build_initial_context(request, db)
    except Exception as exc:
        log.exception("agent_initial_context_error")
        yield f"data: {_json.dumps({'type': 'error', 'message': 'Failed to build initial context'})}\n\n"
        yield f"data: {_json.dumps({'type': 'done', 'latency_ms': int((time.monotonic() - t0) * 1000), 'ok': False})}\n\n"
        return

    sources = _build_sources(initial_modules)
    yield f"data: {_json.dumps({'type': 'sources', 'sources': sources})}\n\n"

    system_prompt = _get_agent_system_prompt(request.query_type)
    user_message = _build_user_message(request, initial_context)

    # Build initial messages list based on backend
    if backend == "openai":
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=settings.anthropic_api_key or "ollama",
            base_url=settings.llm_base_url,
        )
        append_assistant = _openai_append_assistant
        append_tool_results = _openai_append_tool_results
    else:
        messages = [{"role": "user", "content": user_message}]
        from app.core.llm import _make_async_anthropic_client
        client = _make_async_anthropic_client()
        append_assistant = _anthropic_append_assistant
        append_tool_results = _anthropic_append_tool_results

    log.info("agent_start", backend=backend, model=agent_model, query_type=request.query_type, max_turns=max_turns)

    error_message: str | None = None
    answer_text = ""
    turn = 0

    try:
        for turn in range(1, max_turns + 1):
            log.info("agent_turn", turn=turn, backend=backend, tool_calls_so_far=tool_call_count)

            yield f"data: {_json.dumps({'type': 'agent_status', 'message': f'Agent thinking (turn {turn})...', 'turn': turn, 'tool_calls': tool_call_count})}\n\n"

            # --- Execute one turn ---
            # OpenAI: streaming (live reasoning + content tokens)
            # Anthropic: non-streaming (reasoning + content emitted after turn)
            if backend == "openai":
                result = None
                async for event in _openai_turn_stream(client, messages, turn):
                    if isinstance(event, str):
                        # SSE event string — forward directly to client
                        yield event
                    else:
                        # Final result dict
                        result = event
                if result is None:
                    log.warning("agent_empty_stream", turn=turn)
                    break
                # Collect answer text from already-streamed content
                for text in result["text_blocks"]:
                    answer_text += text
            else:
                result = await _anthropic_turn(client, messages, system_prompt)

                # Emit reasoning (Anthropic extended thinking)
                if result.get("reasoning"):
                    reasoning_text = result["reasoning"]
                    yield f"data: {_json.dumps({'type': 'reasoning_start', 'turn': turn})}\n\n"
                    chunk_size = 120
                    for i in range(0, len(reasoning_text), chunk_size):
                        yield f"data: {_json.dumps({'type': 'reasoning', 'token': reasoning_text[i:i+chunk_size], 'turn': turn})}\n\n"
                    yield f"data: {_json.dumps({'type': 'reasoning_end', 'turn': turn})}\n\n"

                # Emit text tokens
                for text in result["text_blocks"]:
                    answer_text += text
                    chunk_size = 80
                    for i in range(0, len(text), chunk_size):
                        yield f"data: {_json.dumps({'type': 'token', 'token': text[i:i+chunk_size]})}\n\n"

            # Execute tool calls if any
            tool_results: list[dict] = []
            for tc in result["tool_calls"]:
                tool_call_count += 1
                input_brief = _json.dumps(tc["input"])[:200]
                input_full = _json.dumps(tc["input"], indent=2)
                yield f"data: {_json.dumps({'type': 'tool_call', 'tool': tc['name'], 'input': input_brief, 'input_full': input_full, 'turn': turn})}\n\n"

                tool_output = await _execute_tool(tc["name"], tc["input"])
                truncated = _truncate_tool_result(tool_output)
                tool_results.append({"tool_use_id": tc["id"], "content": truncated})

                summary = tool_output[:150].replace("\n", " ")
                result_detail = tool_output[:2000]
                yield f"data: {_json.dumps({'type': 'tool_result', 'tool': tc['name'], 'summary': summary, 'detail': result_detail, 'turn': turn})}\n\n"

            # Append assistant + tool results to history
            append_assistant(messages, result["raw_content"])
            if tool_results:
                append_tool_results(messages, tool_results)

            # Stop condition
            if result["stop"]:
                log.info("agent_completed", turns=turn, tool_calls=tool_call_count, backend=backend)
                break
            if not result["tool_calls"] and not result["text_blocks"]:
                log.warning("agent_empty_turn", turn=turn, backend=backend)
                break

            # Wall-clock budget
            elapsed = time.monotonic() - t0
            if elapsed > settings.agent_timeout_seconds:
                log.warning("agent_timeout", elapsed_s=int(elapsed), max_s=settings.agent_timeout_seconds, tool_calls=tool_call_count)
                yield f"data: {_json.dumps({'type': 'token', 'token': f'\\n\\n---\\n*Agent timed out after {int(elapsed)}s. Output may be incomplete.*'})}\n\n"
                break

        else:
            log.warning("agent_max_turns_exceeded", max_turns=max_turns, tool_calls=tool_call_count)
            yield f"data: {_json.dumps({'type': 'token', 'token': f'\\n\\n---\\n*Agent reached maximum of {max_turns} turns. Output may be incomplete.*'})}\n\n"

    except Exception as exc:
        log.exception("agent_loop_error", turn=turn, backend=backend, error=f"{type(exc).__name__}: {exc}")
        error_message = "Internal error while generating response"

    latency_ms = int((time.monotonic() - t0) * 1000)
    if error_message:
        yield f"data: {_json.dumps({'type': 'error', 'message': error_message, 'latency_ms': latency_ms})}\n\n"
    yield f"data: {_json.dumps({'type': 'done', 'latency_ms': latency_ms, 'ok': error_message is None, 'turns': turn, 'tool_calls': tool_call_count})}\n\n"

    try:
        await emit(
            "llm", "agent:compose",
            status="error" if error_message else "success",
            duration_ms=latency_ms,
            request_data={"query": request.query, "query_type": request.query_type},
            response_data={
                "turns": turn,
                "tool_calls": tool_call_count,
                "answer_length": len(answer_text),
            },
            metadata={"model": agent_model, "mode": "agent", "backend": backend},
        )
    except Exception:
        log.warning("agent_audit_emit_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Agent loop — non-streaming
# ---------------------------------------------------------------------------

async def agent_query(request: QueryRequest, db) -> QueryResponse:
    """Non-streaming agent query. Returns a QueryResponse."""
    settings = get_settings()
    t0 = time.monotonic()
    backend = _backend()
    agent_model = _agent_model()
    max_turns = settings.agent_max_turns
    tool_call_count = 0

    initial_context, initial_modules = await _build_initial_context(request, db)
    sources = [
        QueryResult(
            module_name=r["module_name"],
            repo=r["repo"],
            module_path=r["module_path"],
            version=r.get("version", "latest"),
            tags=r["tags"] or [],
            similarity=round(float(r["similarity"]), 4),
            description=r["description"],
        )
        for r in initial_modules
    ]

    system_prompt = _get_agent_system_prompt(request.query_type)
    user_message = _build_user_message(request, initial_context)

    if backend == "openai":
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=settings.anthropic_api_key or "ollama",
            base_url=settings.llm_base_url,
        )
        append_assistant = _openai_append_assistant
        append_tool_results = _openai_append_tool_results
    else:
        messages = [{"role": "user", "content": user_message}]
        from app.core.llm import _make_async_anthropic_client
        client = _make_async_anthropic_client()
        append_assistant = _anthropic_append_assistant
        append_tool_results = _anthropic_append_tool_results

    answer_text = ""
    turn = 0

    for turn in range(1, max_turns + 1):
        if backend == "openai":
            # Non-streaming OpenAI turn (no live reasoning needed for non-stream endpoint)
            from openai import AsyncOpenAI as _AO  # noqa: already imported above
            response = await client.chat.completions.create(
                model=_agent_model(), max_tokens=16384,
                messages=messages, tools=_tools_for_openai(), tool_choice="auto",
            )
            choice = response.choices[0]
            msg = choice.message
            content = _strip_think_tags((msg.content or "").strip())
            text_blocks = [content] if content else []
            tool_calls_parsed: list[dict] = []
            raw_tool_calls: list[dict] = []
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    try:
                        tc_input = _json.loads(tc.function.arguments or "{}")
                    except Exception:
                        tc_input = {}
                    tool_calls_parsed.append({"id": tc.id, "name": tc.function.name, "input": tc_input})
                    raw_tool_calls.append({"id": tc.id, "type": "function",
                                           "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"}})
            stop = choice.finish_reason in ("stop", "length") and not tool_calls_parsed
            result = {"text_blocks": text_blocks, "tool_calls": tool_calls_parsed, "stop": stop,
                      "raw_content": {"content": content, "tool_calls": raw_tool_calls}}
        else:
            result = await _anthropic_turn(client, messages, system_prompt)

        for text in result["text_blocks"]:
            answer_text += text

        tool_results: list[dict] = []
        for tc in result["tool_calls"]:
            tool_call_count += 1
            tool_output = await _execute_tool(tc["name"], tc["input"])
            tool_results.append({"tool_use_id": tc["id"], "content": _truncate_tool_result(tool_output)})

        append_assistant(messages, result["raw_content"])
        if tool_results:
            append_tool_results(messages, tool_results)

        if result["stop"]:
            break
        if not result["tool_calls"] and not result["text_blocks"]:
            break
        elapsed = time.monotonic() - t0
        if elapsed > settings.agent_timeout_seconds:
            log.warning("agent_timeout", elapsed_s=int(elapsed), max_s=settings.agent_timeout_seconds)
            break

    latency_ms = int((time.monotonic() - t0) * 1000)

    await emit(
        "llm", "agent:compose",
        status="success",
        duration_ms=latency_ms,
        request_data={"query": request.query, "query_type": request.query_type},
        response_data={"turns": turn, "tool_calls": tool_call_count, "answer_length": len(answer_text)},
        metadata={"model": agent_model, "mode": "agent", "backend": backend},
    )

    return QueryResponse(answer=answer_text, sources=sources, latency_ms=latency_ms)
