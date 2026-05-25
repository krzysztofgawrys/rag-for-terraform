"""
Unified LLM client.

Operating mode depends on .env:
  - llm_base_url=""          → Anthropic SDK (directly to api.anthropic.com)
  - aws_bedrock_region set   → Anthropic via AWS Bedrock
  - llm_base_url="https://openrouter.ai/api/v1"  → OpenRouter (OpenAI-compatible)
  - llm_base_url="http://ollama:11434/v1"         → Ollama locally

LLM_CONCURRENT_PROMPTS controls parallelism (default 1 = sequential).
LLM_MAX_RETRIES controls how many times transient errors are retried.
"""
from __future__ import annotations
import asyncio
import re
import time
import structlog
from app.core.config import get_settings

log = structlog.get_logger()
settings = get_settings()


def _use_bedrock() -> bool:
    return bool(settings.aws_bedrock_region and not settings.llm_base_url)


def _has_anthropic_backend() -> bool:
    """True when either direct Anthropic API key or Bedrock region is configured."""
    return bool(settings.anthropic_api_key or _use_bedrock())


def _bedrock_session():
    """Create a boto3 Session for Bedrock (profile / role / default)."""
    import boto3
    session = boto3.Session(
        profile_name=settings.aws_bedrock_profile or None,
        region_name=settings.aws_bedrock_region,
    )
    if settings.aws_bedrock_role_arn:
        creds = session.client("sts").assume_role(
            RoleArn=settings.aws_bedrock_role_arn,
            RoleSessionName="terraform-rag",
        )["Credentials"]
        session = boto3.Session(
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            aws_session_token=creds["SessionToken"],
            region_name=settings.aws_bedrock_region,
        )
    return session


def _bedrock_client():
    """Create a boto3 bedrock-runtime client."""
    return _bedrock_session().client("bedrock-runtime")


def _bedrock_converse(prompt: str, system: str, max_tokens: int,
                      model: str = "") -> str:
    """Call Bedrock Converse API (works with any model: Anthropic, Mistral, Nova, etc.)."""
    client = _bedrock_client()
    kwargs: dict = {
        "modelId": model or settings.llm_model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    response = client.converse(**kwargs)
    return response["output"]["message"]["content"][0]["text"].strip()


def _bedrock_converse_stream(prompt: str, system: str, max_tokens: int,
                             model: str = ""):
    """Streaming Bedrock Converse — yields text chunks."""
    client = _bedrock_client()
    kwargs: dict = {
        "modelId": model or settings.llm_model,
        "messages": [{"role": "user", "content": [{"text": prompt}]}],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if system:
        kwargs["system"] = [{"text": system}]
    response = client.converse_stream(**kwargs)
    for event in response["stream"]:
        delta = event.get("contentBlockDelta", {}).get("delta", {}).get("text")
        if delta:
            yield delta


def _make_anthropic_client():
    """Create sync Anthropic client — Bedrock or direct API depending on config."""
    import anthropic
    if _use_bedrock():
        return anthropic.AnthropicBedrock(
            aws_region=settings.aws_bedrock_region,
        )
    return anthropic.Anthropic(api_key=settings.anthropic_api_key)


def _make_async_anthropic_client():
    """Create async Anthropic client — Bedrock or direct API depending on config."""
    import anthropic
    if _use_bedrock():
        return anthropic.AsyncAnthropicBedrock(
            aws_region=settings.aws_bedrock_region,
        )
    return anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

_semaphores: dict[int, asyncio.Semaphore] = {}

# HTTP status codes that are safe to retry (transient errors).
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}


def _get_semaphore() -> asyncio.Semaphore:
    """Get a semaphore bound to the current event loop (safe for Celery workers)."""
    loop = asyncio.get_running_loop()
    loop_id = id(loop)
    if loop_id not in _semaphores:
        _semaphores[loop_id] = asyncio.Semaphore(settings.llm_concurrent_prompts)
    return _semaphores[loop_id]


def _is_retryable(exc: Exception) -> bool:
    """Check if an exception is a transient API error worth retrying."""
    # Anthropic SDK errors
    status = getattr(exc, "status_code", None)
    if status and status in _RETRYABLE_STATUS_CODES:
        return True
    # OpenAI SDK errors
    if hasattr(exc, "status_code"):
        return exc.status_code in _RETRYABLE_STATUS_CODES
    # Generic HTTP errors embedded in message
    msg = str(exc).lower()
    if any(f"{code}" in msg for code in _RETRYABLE_STATUS_CODES):
        return True
    # Connection errors
    if "connection" in msg or "timeout" in msg:
        return True
    return False


def complete(prompt: str, system: str = "", max_tokens: int = 1000) -> str:
    """
    Send a single query to LLM (synchronously).
    If no API key / Bedrock — returns empty string (fallback in calling code).
    """
    if not _has_anthropic_backend():
        return ""

    if settings.llm_base_url:
        return _openai_compatible(prompt, system, max_tokens)
    elif _use_bedrock():
        return _bedrock_converse(prompt, system, max_tokens)
    else:
        return _anthropic(prompt, system, max_tokens)


async def acomplete(prompt: str, system: str = "", max_tokens: int = 1000) -> str:
    """Async LLM call with concurrency control and retry on transient errors."""
    start = time.perf_counter()
    result = ""
    error_text = None
    max_retries = settings.llm_max_retries
    async with _get_semaphore():
        try:
            for attempt in range(max_retries + 1):
                try:
                    result = await asyncio.to_thread(complete, prompt, system, max_tokens)
                    return result
                except Exception as exc:
                    if attempt < max_retries and _is_retryable(exc):
                        delay = 2 ** attempt  # 1s, 2s, 4s
                        log.warning("llm_retry", attempt=attempt + 1,
                                    max_retries=max_retries, delay=delay,
                                    error=str(exc)[:200])
                        await asyncio.sleep(delay)
                        continue
                    raise
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            from app.core.audit import emit
            await emit(
                "llm", "llm:acomplete",
                status="error" if error_text else "success",
                duration_ms=int((time.perf_counter() - start) * 1000),
                request_data={"prompt": prompt, "system": system, "max_tokens": max_tokens},
                response_data={"response": result},
                error=error_text,
                metadata={"model": settings.llm_model},
            )


def describe(prompt: str, system: str = "", max_tokens: int = 300) -> str:
    """LLM call for module descriptions — uses dedicated model if configured."""
    api_key = settings.description_llm_api_key or settings.anthropic_api_key
    if not api_key and not _use_bedrock():
        return ""
    base_url = settings.description_llm_base_url or settings.llm_base_url
    model = settings.description_llm_model or settings.llm_model
    temp = settings.description_llm_temperature
    if base_url:
        return _openai_compatible(prompt, system, max_tokens,
                                  api_key=api_key, base_url=base_url, model=model,
                                  temperature=temp)
    elif _use_bedrock():
        return _bedrock_converse(prompt, system, max_tokens, model=model)
    else:
        return _anthropic(prompt, system, max_tokens,
                          api_key=api_key, model=model, use_thinking=False)


async def adescribe(prompt: str, system: str = "", max_tokens: int = 300) -> str:
    """Async version of describe() with retry on transient errors."""
    start = time.perf_counter()
    result = ""
    error_text = None
    model = settings.description_llm_model or settings.llm_model
    max_retries = settings.llm_max_retries
    async with _get_semaphore():
        try:
            for attempt in range(max_retries + 1):
                try:
                    result = await asyncio.to_thread(describe, prompt, system, max_tokens)
                    return result
                except Exception as exc:
                    if attempt < max_retries and _is_retryable(exc):
                        delay = 2 ** attempt
                        log.warning("llm_retry", attempt=attempt + 1,
                                    max_retries=max_retries, delay=delay,
                                    error=str(exc)[:200], caller="describe")
                        await asyncio.sleep(delay)
                        continue
                    raise
        except Exception as exc:
            error_text = str(exc)
            raise
        finally:
            from app.core.audit import emit
            await emit(
                "llm", "llm:adescribe",
                status="error" if error_text else "success",
                duration_ms=int((time.perf_counter() - start) * 1000),
                request_data={"prompt": prompt, "system": system, "max_tokens": max_tokens},
                response_data={"response": result},
                error=error_text,
                metadata={"model": model, "caller": "describe"},
            )


_THINK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models (DeepSeek R1, etc.)."""
    return _THINK_RE.sub("", text).strip()


# -- Anthropic SDK -------------------------------------------------------------

def _anthropic(prompt: str, system: str, max_tokens: int,
               api_key: str = "", model: str = "",
               use_thinking: bool = True) -> str:
    """Sync Anthropic direct-API call. Uses streaming internally so we don't
    trip the SDK's 10-minute guard (ValueError if a non-streaming request
    *might* exceed 10 min — happens with large max_tokens + thinking).
    """
    import anthropic
    client = anthropic.Anthropic(api_key=api_key or settings.anthropic_api_key)
    messages = [{"role": "user", "content": prompt}]
    kwargs = dict(
        model=model or settings.llm_model,
        max_tokens=max_tokens,
        messages=messages,
    )
    if use_thinking and settings.llm_thinking_budget > 0:
        budget = settings.llm_thinking_budget
        kwargs["max_tokens"] = budget + max_tokens
        kwargs["temperature"] = 1
        kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
    if system:
        kwargs["system"] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
        ]

    accumulated: list[str] = []
    with client.messages.stream(**kwargs) as stream:
        for text in stream.text_stream:
            accumulated.append(text)
    return "".join(accumulated).strip()


# -- OpenAI-compatible (OpenRouter, Ollama, etc.) ------------------------------

def _openai_compatible(prompt: str, system: str, max_tokens: int,
                       api_key: str = "", base_url: str = "",
                       model: str = "", temperature: float | None = None) -> str:
    from openai import OpenAI
    client = OpenAI(
        api_key=api_key or settings.anthropic_api_key,
        base_url=base_url or settings.llm_base_url,
    )
    messages = _build_openai_messages(prompt, system)

    kwargs = dict(
        model=model or settings.llm_model,
        max_tokens=max_tokens,
        messages=messages,
    )
    if temperature is not None:
        kwargs["temperature"] = temperature

    response = client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    text = (msg.content or "").strip()
    # Reasoning models (Qwen3, etc.) may put the answer in reasoning_content
    if not text:
        reasoning = getattr(msg, "reasoning_content", None) or ""
        text = reasoning.strip()
    return _strip_think_tags(text)


def _build_openai_messages(prompt: str, system: str) -> list[dict]:
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    return messages


# -- Streaming ----------------------------------------------------------------

async def astream(prompt: str, system: str = "", max_tokens: int = 2000):
    """Async generator yielding text chunks from LLM."""
    start = time.perf_counter()
    accumulated: list[str] = []
    error_text = None
    try:
        if not _has_anthropic_backend():
            yield ""
            return

        if settings.llm_base_url:
            async for chunk in _astream_openai(prompt, system, max_tokens):
                accumulated.append(chunk)
                yield chunk
        elif _use_bedrock():
            async for chunk in _astream_bedrock(prompt, system, max_tokens):
                accumulated.append(chunk)
                yield chunk
        else:
            async for chunk in _astream_anthropic(prompt, system, max_tokens):
                accumulated.append(chunk)
                yield chunk
    except Exception as exc:
        error_text = str(exc)
        raise
    finally:
        from app.core.audit import emit
        await emit(
            "llm", "llm:astream",
            status="error" if error_text else "success",
            duration_ms=int((time.perf_counter() - start) * 1000),
            request_data={"prompt": prompt, "system": system, "max_tokens": max_tokens},
            response_data={"response": "".join(accumulated), "chunk_count": len(accumulated)},
            error=error_text,
            metadata={"model": settings.llm_model, "streaming": True},
        )


async def _astream_openai(prompt: str, system: str, max_tokens: int):
    import asyncio
    from openai import OpenAI

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _sync_stream():
        client = OpenAI(
            api_key=settings.anthropic_api_key,
            base_url=settings.llm_base_url,
        )
        messages = _build_openai_messages(prompt, system)
        stream = client.chat.completions.create(
            model=settings.llm_model,
            max_tokens=max_tokens,
            messages=messages,
            stream=True,
        )
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices[0].delta else None
            if not delta and chunk.choices[0].delta:
                delta = getattr(chunk.choices[0].delta, "reasoning_content", None)
            if delta:
                loop.call_soon_threadsafe(queue.put_nowait, delta)
        loop.call_soon_threadsafe(queue.put_nowait, None)  # sentinel

    asyncio.get_event_loop().run_in_executor(None, _sync_stream)

    # Buffer and strip <think>...</think> blocks from reasoning models.
    buf = ""
    inside_think = False
    while True:
        chunk = await queue.get()
        if chunk is None:
            # Flush remaining buffer (in case of unclosed <think> tag)
            if buf and not inside_think:
                yield buf
            break
        buf += chunk
        if inside_think:
            close_idx = buf.find("</think>")
            if close_idx != -1:
                buf = buf[close_idx + len("</think>"):]
                buf = buf.lstrip()
                inside_think = False
            else:
                continue  # still inside think block, keep buffering
        # Check if a <think> tag starts in the buffer
        while not inside_think:
            think_idx = buf.find("<think>")
            if think_idx == -1:
                # No <think> tag — emit everything except a trailing partial "<"
                safe_end = buf.rfind("<")
                if safe_end > 0 and safe_end > len(buf) - 8:
                    # Could be start of "<think>", hold back
                    yield buf[:safe_end]
                    buf = buf[safe_end:]
                else:
                    yield buf
                    buf = ""
                break
            else:
                # Emit text before <think>
                if think_idx > 0:
                    yield buf[:think_idx]
                buf = buf[think_idx + len("<think>"):]
                inside_think = True
                # Check if close tag is already in buffer
                close_idx = buf.find("</think>")
                if close_idx != -1:
                    buf = buf[close_idx + len("</think>"):]
                    buf = buf.lstrip()
                    inside_think = False


async def _astream_bedrock(prompt: str, system: str, max_tokens: int):
    """Async generator wrapping sync boto3 converse_stream via thread executor."""
    import asyncio

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _sync_stream():
        for chunk in _bedrock_converse_stream(prompt, system, max_tokens):
            loop.call_soon_threadsafe(queue.put_nowait, chunk)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.get_event_loop().run_in_executor(None, _sync_stream)
    while True:
        chunk = await queue.get()
        if chunk is None:
            break
        yield chunk


async def _astream_anthropic(prompt: str, system: str, max_tokens: int):
    import asyncio
    import anthropic

    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_event_loop()

    def _sync_stream():
        client = _make_anthropic_client()
        messages = [{"role": "user", "content": prompt}]
        kwargs = dict(
            model=settings.llm_model, max_tokens=max_tokens,
            messages=messages,
        )
        if settings.llm_thinking_budget > 0:
            budget = settings.llm_thinking_budget
            kwargs["max_tokens"] = budget + max_tokens
            kwargs["temperature"] = 1
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
        if system:
            kwargs["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}},
            ]
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                loop.call_soon_threadsafe(queue.put_nowait, text)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    asyncio.get_event_loop().run_in_executor(None, _sync_stream)
    while True:
        chunk = await queue.get()
        if chunk is None:
            break
        yield chunk
