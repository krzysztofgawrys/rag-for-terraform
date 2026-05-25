import json
import structlog
from functools import lru_cache
from app.core.config import get_settings
from app.core.parser import ParsedModule
from app.prompts import load_prompt_sections

log = structlog.get_logger()
settings = get_settings()

# Instruction prompts for models that support them (EmbeddingGemma, Snowflake)
_embedding_prompts = load_prompt_sections("embeddings.md")
_DOC_PROMPT = _embedding_prompts["doc_prompt"] + " "

_QUERY_PROMPTS: dict[str, str] = {
    k.removeprefix("query_"): v + " "
    for k, v in _embedding_prompts.items()
    if k.startswith("query_")
}
_DEFAULT_QUERY_PROMPT = _QUERY_PROMPTS["search"]


# ---------------------------------------------------------------------------
# Provider: local (sentence-transformers)
# ---------------------------------------------------------------------------

def _get_local_model():
    from sentence_transformers import SentenceTransformer
    return _load_local_model()


@lru_cache(maxsize=1)
def _load_local_model():
    from sentence_transformers import SentenceTransformer
    log.info("loading_embedding_model", model=settings.embedding_model)
    return SentenceTransformer(settings.embedding_model, trust_remote_code=True)


def _supports_prompts() -> bool:
    model = _get_local_model()
    name = settings.embedding_model.lower()
    return hasattr(model, "prompts") or "gemma" in name or "snowflake" in name


def _embed_local(text: str, prompt: str | None = None) -> list[float]:
    model = _get_local_model()
    kwargs: dict = {"normalize_embeddings": True}
    if prompt and _supports_prompts():
        kwargs["prompt"] = prompt
    vector = model.encode(text, **kwargs)
    return vector.tolist()


# ---------------------------------------------------------------------------
# Provider: bedrock (Amazon Titan Embed V2)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_bedrock_client():
    import boto3
    region = settings.embedding_bedrock_region or settings.aws_bedrock_region
    if not region:
        raise ValueError(
            "embedding_bedrock_region or aws_bedrock_region must be set "
            "when embedding_provider=bedrock"
        )
    session_kwargs: dict = {}
    if settings.aws_bedrock_profile:
        session_kwargs["profile_name"] = settings.aws_bedrock_profile
    session = boto3.Session(region_name=region, **session_kwargs)
    client = session.client("bedrock-runtime")
    log.info(
        "bedrock_embedding_client_ready",
        model=settings.embedding_bedrock_model_id,
        region=region,
    )
    return client


_BEDROCK_MAX_CHARS = 30_000  # ~8k tokens at ~4 chars/token for Titan Embed V2


def _embed_bedrock(text: str) -> list[float]:
    client = _get_bedrock_client()
    if len(text) > _BEDROCK_MAX_CHARS:
        log.warning("embed_text_truncated", original_len=len(text),
                    max_len=_BEDROCK_MAX_CHARS)
        text = text[:_BEDROCK_MAX_CHARS]
    body = json.dumps({
        "inputText": text,
        "dimensions": settings.embedding_dim,
        "normalize": True,
    })
    response = client.invoke_model(
        modelId=settings.embedding_bedrock_model_id,
        contentType="application/json",
        accept="application/json",
        body=body,
    )
    result = json.loads(response["body"].read())
    return result["embedding"]


# ---------------------------------------------------------------------------
# Public API (unchanged signatures)
# ---------------------------------------------------------------------------

def embed_module(module: ParsedModule, description: str) -> list[float]:
    """
    Build embedding text from multiple module signals —
    not just raw code, but also structure (better quality).
    """
    text = _build_embedding_text(module, description)
    if settings.embedding_provider == "bedrock":
        return _embed_bedrock(text)
    return _embed_local(text, prompt=_DOC_PROMPT)


def embed_query(query: str, query_type: str = "search") -> list[float]:
    if settings.embedding_provider == "bedrock":
        return _embed_bedrock(query)
    prompt = _QUERY_PROMPTS.get(query_type, _DEFAULT_QUERY_PROMPT)
    return _embed_local(query, prompt=prompt)


def _build_embedding_text(module: ParsedModule, description: str) -> str:
    """
    Combine semantic description + metadata + code snippet.
    LLM-generated description significantly improves search quality.
    """
    var_lines = "\n".join(
        f"  - {name} ({cfg.get('type','any')})"
        + (f": {cfg['description']}" if cfg.get("description") else "")
        for name, cfg in module.variables.items()
    )
    out_lines = "\n".join(
        f"  - {name}" + (f": {cfg['description']}" if cfg.get("description") else "")
        for name, cfg in module.outputs.items()
    )

    return f"""
Module: {module.module_name}
Repository: {module.repo}
Tags: {', '.join(module.tags)}
Resources: {', '.join(set(module.resources))}
Description: {description}

Input variables:
{var_lines or '  (none)'}

Outputs:
{out_lines or '  (none)'}

Code excerpt:
{module.raw_code[:5000]}
""".strip()
