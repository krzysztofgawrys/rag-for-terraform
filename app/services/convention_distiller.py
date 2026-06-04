"""
Convention Distiller — extracts convention snippets from usage data.

Takes N usage summaries for a given module_ref and distills them into
convention snippets (one per dimension) using a cheap LLM.

Round 9 params:
  - MAX_TOKENS = 4000 (sufficient for convention paragraphs)
  - No char ceiling — model writes as much as the data warrants
  - No retry on LLM failure
  - System prompt: comprehensive, density-as-needed
  - Few-shots: variable length per dimension (richer for vars, codeploy)
  - Preserved: stats, thresholds, ASSESSMENT, strip preamble
  - describe() no longer applies extended thinking (avoids streaming requirement)
"""

import re
import structlog
from app.core.config import get_settings
from app.core import llm
from app.prompts import load_prompt, load_prompt_sections

log = structlog.get_logger()
settings = get_settings()

MAX_TOKENS = 4000

# -- Dimensions --------------------------------------------------------------

DIMENSIONS = [
    "naming",
    "vars",
    "codeploy",
    "tagging",
    "layout",
    "versions",
]

# -- System prompt -----------------------------------------------------------

SYSTEM_PROMPT = load_prompt("distiller/system.md")

# -- Few-shot examples per dimension -----------------------------------------

FEW_SHOTS: dict[str, dict[str, str]] = {}
for _dim in DIMENSIONS:
    _sections = load_prompt_sections(f"distiller/fewshot_{_dim}.md")
    if "input" in _sections and "output" in _sections:
        FEW_SHOTS[_dim] = {"input": _sections["input"], "output": _sections["output"]}


def build_distillation_prompt(
    module_ref: str,
    dimension: str,
    usage_summaries: list[str],
) -> str:
    """Build the user prompt for distilling one dimension.

    Sends the full usage list to the LLM (no sampling) — modern context
    windows easily fit hundreds of one-line summaries, and full evidence
    produces stronger conventions than a 15-entry sample.
    """
    few = FEW_SHOTS.get(dimension)
    few_shot_block = ""
    if few:
        few_shot_block = (
            f"\n--- EXAMPLE ---\n"
            f"Input:\n{few['input']}\n\n"
            f"Output:\n{few['output']}\n"
            f"--- END EXAMPLE ---\n\n"
        )

    usage_block = "\n".join(f"- {s}" for s in usage_summaries)
    stats_line = f"({len(usage_summaries)} usages total)"

    return (
        f"Extract the **{dimension}** convention for module '{module_ref}'.\n\n"
        f"{few_shot_block}"
        f"Now analyze the real data:\n\n"
        f"Module: {module_ref} {stats_line}\n"
        f"Usage summaries:\n{usage_block}\n\n"
        f"Write the {dimension} convention paragraph. "
        f"Remember: end with an ASSESSMENT line."
    )


def strip_preamble(text: str) -> str:
    """Remove LLM preamble like 'Here is the convention:' etc."""
    # Strip common preamble patterns
    patterns = [
        r"^(?:Here(?:'s| is) (?:the|my|a) .*?(?:convention|analysis|paragraph)[:\.]?\s*\n?)",
        r"^(?:Based on (?:the|these) .*?(?:usages?|summaries?|data)[,:\.]?\s*\n?)",
        r"^(?:(?:The |A )?(?:convention|analysis|paragraph) (?:for|is)[:\.]?\s*\n?)",
        r"^(?:\*\*.*?\*\*\s*\n?)",  # bold headers
        r"^(?:#{1,3}\s+.*?\n)",  # markdown headers
    ]
    result = text.strip()
    for pattern in patterns:
        result = re.sub(pattern, "", result, flags=re.IGNORECASE).strip()
    return result


def extract_assessment(text: str) -> tuple[str, str]:
    """Split text into (content, assessment).

    Takes the FIRST ASSESSMENT line — anything after it (duplicated blocks,
    trailing remarks) is discarded.  Returns (text, 'UNKNOWN') if no
    assessment found.
    """
    # Use re.search (finds first match) — not findall/finditer
    m = re.search(r'\n?ASSESSMENT:\s*(\w+)', text, re.IGNORECASE)
    if m:
        assessment = m.group(1).upper()
        content = text[:m.start()].strip()
        return content, assessment
    return text.strip(), "UNKNOWN"


EVAL_SYSTEM_PROMPT = load_prompt("distiller/eval.md")


async def evaluate_distillation(
    convention: str,
    dimension: str,
    usage_summaries: list[str],
) -> tuple[int, str]:
    """Self-evaluate a distilled convention against the source usages.

    Sees the same full usage list the distiller saw — no sampling.
    Returns (score 1-5, reason). On LLM failure returns (3, "eval_failed").
    """
    import json as _json

    usage_block = "\n".join(f"- {s}" for s in usage_summaries)

    prompt = (
        f"Dimension: {dimension}\n\n"
        f"Convention paragraph:\n{convention}\n\n"
        f"Source usage summaries ({len(usage_summaries)} total):\n{usage_block}"
    )

    raw = await llm.adescribe(prompt, system=EVAL_SYSTEM_PROMPT, max_tokens=200)
    if not raw:
        return 3, "eval_failed"

    # Parse JSON response
    try:
        # Strip markdown fences if present
        cleaned = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = _json.loads(cleaned)
        score = int(data.get("score", 3))
        reason = str(data.get("reason", ""))
        return max(1, min(5, score)), reason
    except (ValueError, _json.JSONDecodeError):
        log.warning("eval_parse_failed", raw=raw[:200])
        return 3, "eval_parse_failed"


async def distill_dimension(
    module_ref: str,
    dimension: str,
    usage_summaries: list[str],
) -> dict:
    """Distill one convention dimension from usage summaries.

    Returns dict with keys: dimension, summary, assessment, evidence_count, char_count.
    Returns None if LLM fails or insufficient data.
    """
    if len(usage_summaries) < 1:
        return {
            "dimension": dimension,
            "summary": f"Insufficient data for {dimension} convention ({len(usage_summaries)} usage(s)).",
            "assessment": "INSUFFICIENT_DATA",
            "evidence_count": len(usage_summaries),
            "char_count": 0,
        }

    prompt = build_distillation_prompt(module_ref, dimension, usage_summaries)
    log.info("distilling", module_ref=module_ref, dimension=dimension,
             usages=len(usage_summaries), prompt_len=len(prompt))

    raw = await llm.adescribe(prompt, system=SYSTEM_PROMPT, max_tokens=MAX_TOKENS)

    if not raw:
        log.warning("distill_empty_response", module_ref=module_ref, dimension=dimension)
        return None

    # Strip preamble
    cleaned = strip_preamble(raw)

    # Extract assessment
    content, assessment = extract_assessment(cleaned)

    # Guard: if content is empty after cleanup, treat as failed distillation
    if not content.strip():
        log.warning("distill_empty_content", module_ref=module_ref,
                     dimension=dimension, raw_len=len(raw))
        return None

    # Self-evaluate: cross-check convention against source usages
    eval_score, eval_reason = await evaluate_distillation(
        content, dimension, usage_summaries,
    )

    log.info("distilled", module_ref=module_ref, dimension=dimension,
             assessment=assessment, chars=len(content),
             eval_score=eval_score, eval_reason=eval_reason)

    return {
        "dimension": dimension,
        "summary": content,
        "assessment": assessment,
        "evidence_count": len(usage_summaries),
        "char_count": len(content),
        "eval_score": eval_score,
        "eval_reason": eval_reason,
    }


async def distill_all_dimensions(
    module_ref: str,
    usage_summaries: list[str],
) -> list[dict]:
    """Distill all convention dimensions for a module in parallel.

    Errors in individual dimensions are logged and skipped — remaining
    dimensions still get processed. Parallelism is bounded by the
    LLM_CONCURRENT_PROMPTS semaphore.
    """
    import asyncio

    async def _safe_distill(dim: str) -> dict | None:
        try:
            return await distill_dimension(module_ref, dim, usage_summaries)
        except Exception:
            log.exception("distill_dimension_error",
                          module_ref=module_ref, dimension=dim)
            return None

    raw_results = await asyncio.gather(*[_safe_distill(dim) for dim in DIMENSIONS])
    return [r for r in raw_results if r]


async def run_distillation(
    module_refs: list[str],
    job_id: str | None = None,
    base_stats: dict | None = None,
) -> dict:
    """Full distillation pipeline: read usage from DB → distill → write conventions.

    Called after consumer indexing to update convention snippets for affected modules.
    If job_id is given, updates consumer_index_jobs.stats incrementally.
    base_stats are the stats from the indexing phase (parsed, resolved, embedded).
    Returns distillation stats dict.
    """
    from app.core.vector_store import (
        make_session_factory, get_usage_summaries, upsert_snippet,
        update_consumer_index_job, mark_snippet_stale,
        get_existing_convention_quality,
    )
    from app.core.embeddings import embed_query
    import json

    engine, SessionLocal = make_session_factory()
    distill_stats = {
        "modules": 0, "dimensions": 0, "skipped": 0,
        "stale_marked": 0, "kept_existing": 0, "llm_failed": 0,
    }

    try:
        async with SessionLocal() as db:
            for module_ref in module_refs:
                try:
                    await _distill_one_module(
                        db, module_ref, distill_stats,
                        embed_query, upsert_snippet, mark_snippet_stale,
                        get_existing_convention_quality,
                    )
                except Exception:
                    log.exception("distill_module_error", module_ref=module_ref)
                    distill_stats["skipped"] += 1
                    # Continue with next module — don't abort the whole run

                # Update job stats after each module
                if job_id:
                    merged = {**(base_stats or {}), "distillation": distill_stats}
                    await update_consumer_index_job(
                        db, job_id,
                        stats=json.dumps(merged),
                    )
    finally:
        await engine.dispose()

    log.info("distillation_complete", **distill_stats)
    return distill_stats


async def _distill_one_module(
    db,
    module_ref: str,
    distill_stats: dict,
    embed_query,
    upsert_snippet,
    mark_snippet_stale,
    get_existing_convention_quality,
) -> None:
    """Distill all dimensions for a single module and persist results."""
    from app.core.vector_store import (
        get_usage_summaries, mark_module_conventions_stale,
    )

    usages = await get_usage_summaries(db, module_ref)
    if len(usages) < 1:
        # Module lost all usage — mark its conventions as stale
        marked = await mark_module_conventions_stale(db, module_ref)
        if marked:
            log.info("distill_orphaned_stale", module_ref=module_ref,
                     conventions_marked=marked)
            distill_stats["stale_marked"] += marked
        else:
            log.info("distill_skip_no_usage", module_ref=module_ref)
        distill_stats["skipped"] += 1
        return

    log.info("distilling_module", module_ref=module_ref, usages=len(usages))
    results = await distill_all_dimensions(module_ref, usages)

    # Detect LLM failures: distill_dimension returns None on empty/throttled
    # response, then distill_all_dimensions filters those out. The diff between
    # attempted (= |DIMENSIONS|) and returned len reveals dimensions that the
    # LLM dropped (rate limiting, daily token cap, transient API errors). Without
    # tracking this we silently report `modules: N, dimensions: 0` and conventions
    # appear "processed" while nothing was actually written.
    attempted = len(DIMENSIONS)
    returned = len(results)
    llm_dropped = attempted - returned
    if llm_dropped:
        distill_stats["llm_failed"] += llm_dropped
        log.warning("distill_llm_dropped_dimensions",
                    module_ref=module_ref,
                    dropped=llm_dropped, attempted=attempted)

    if returned == 0:
        # Every dimension failed at LLM level - do NOT count this as a processed
        # module, otherwise stats falsely show modules > 0 with dimensions = 0.
        log.warning("distill_module_total_llm_failure",
                    module_ref=module_ref, attempted=attempted)
        distill_stats["skipped"] += 1
        return

    for r in results:
        if not r or r["assessment"] == "INSUFFICIENT_DATA":
            continue

        # Skip conventions with empty summary (LLM returned only assessment
        # or content was stripped entirely during cleanup)
        if not r.get("summary", "").strip():
            log.warning("distill_skip_empty_summary",
                        module_ref=module_ref, dimension=r["dimension"])
            continue

        kind = f"convention.{r['dimension']}"
        new_score = r.get("eval_score", 3)

        # Quality gate: eval_score < 3 → skip upsert.
        # Only mark existing as stale if there's no good existing
        # convention to preserve.
        if new_score < 3:
            existing_score, _ = (
                await get_existing_convention_quality(
                    db, module_ref, kind,
                )
            )
            if existing_score is not None and existing_score >= 3:
                # Existing convention is good — keep it
                log.info(
                    "distill_quality_gate_kept_existing",
                    module_ref=module_ref, dimension=r["dimension"],
                    new_score=new_score,
                    existing_score=existing_score,
                )
                distill_stats["kept_existing"] += 1
            else:
                log.warning(
                    "distill_quality_gate_failed",
                    module_ref=module_ref, dimension=r["dimension"],
                    eval_score=new_score,
                    eval_reason=r.get("eval_reason", ""),
                )
                marked = await mark_snippet_stale(db, module_ref, kind)
                if marked:
                    log.info("snippet_marked_stale",
                             module_ref=module_ref, kind=kind)
                    distill_stats["stale_marked"] += 1
            continue

        # Quality protection: keep existing convention if it scored
        # higher, unless the new one has more evidence (more usages
        # available since last distillation).
        existing_score, existing_evidence = (
            await get_existing_convention_quality(db, module_ref, kind)
        )
        if (existing_score is not None
                and new_score < existing_score
                and r["evidence_count"] <= (existing_evidence or 0)):
            log.info(
                "distill_kept_existing",
                module_ref=module_ref, dimension=r["dimension"],
                new_score=new_score,
                existing_score=existing_score,
                existing_evidence=existing_evidence,
            )
            distill_stats["kept_existing"] += 1
            continue

        embedding = embed_query(r["summary"], query_type="search")

        await upsert_snippet(
            db,
            kind=kind,
            module_ref=module_ref,
            summary=r["summary"],
            embedding=embedding,
            evidence_count=r["evidence_count"],
            eval_score=new_score,
        )
        distill_stats["dimensions"] += 1

    distill_stats["modules"] += 1
    log.info("distilled_module", module_ref=module_ref,
             dimensions=len(results))
