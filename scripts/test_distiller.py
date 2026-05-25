#!/usr/bin/env python3
"""
Test script for convention_distiller.py — Round 6.

Connects to the running postgres, picks 3 real modules with rich data,
synthesizes realistic usage summaries from their variables/tags/resources,
then runs distillation through qwen for all 6 dimensions.

Usage (from project root, inside the api container or with direct DB access):
    docker compose exec api python -m scripts.test_distiller

Or standalone with POSTGRES_HOST=localhost:
    POSTGRES_HOST=localhost python scripts/test_distiller.py
"""

import asyncio
import json
import os
import sys
import time
import random

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# -- Synthetic usage generation ----------------------------------------------

ENVS = ["prod", "staging", "dev"]
REGIONS = ["eu-west-1", "us-east-1", "ap-southeast-1"]
REGION_SHORT = {"eu-west-1": "eu", "us-east-1": "us", "ap-southeast-1": "ap"}
CONSUMER_REPOS = ["tf-infra-prod", "tf-infra-staging", "tf-infra-dev", "tf-infra-legacy"]


def generate_usage_summaries(module: dict, count: int = 15) -> list[str]:
    """Generate realistic usage summaries from real module data."""
    name = module["module_name"]
    repo = module["repo"]
    path = module["module_path"]
    variables = module.get("variables") or {}
    resources = module.get("resources") or []
    tags_list = module.get("tags") or []
    version = module.get("version", "master")

    # Get variable names
    var_names = list(variables.keys()) if isinstance(variables, dict) else []
    required_vars = [
        v for v, cfg in variables.items()
        if isinstance(cfg, dict) and cfg.get("required", False)
    ] if isinstance(variables, dict) else []
    optional_vars = [v for v in var_names if v not in required_vars]

    # Generate fake versions
    versions = [f"v{random.randint(1,3)}.{random.randint(0,5)}.{random.randint(0,3)}" for _ in range(4)]
    versions = sorted(set(versions), reverse=True) or ["v1.0.0"]

    # Pick some sibling modules (co-deploy candidates)
    siblings = random.sample(
        ["shared/vpc", "shared/iam", "core/alb", "core/certificate",
         "core/elasticache", "core/elasticsearch", "core/ecr", "core/ecs_service"],
        k=min(3, random.randint(1, 4))
    )

    summaries = []
    for i in range(count):
        env = random.choices(ENVS, weights=[0.5, 0.3, 0.2])[0]
        region = random.choice(REGIONS)
        region_short = REGION_SHORT[region]
        ver = random.choice(versions)
        consumer = f"tf-infra-{env}"

        # Instance name
        instance_name = f"{name}-{env}-{region_short}"

        # Variables — always pass required, randomly pass optional
        passed_vars = list(required_vars)
        for ov in optional_vars:
            if random.random() < 0.4:
                passed_vars.append(ov)

        # Build var string with some literal values
        var_parts = []
        for v in passed_vars[:8]:  # cap at 8 for readability
            cfg = variables.get(v, {})
            if isinstance(cfg, dict):
                default = cfg.get("default")
                if default is not None and not isinstance(default, (dict, list)):
                    var_parts.append(f"{v}='{default}'")
                elif v in ("env", "environment"):
                    var_parts.append(f"{v}='{env}'")
                elif "name" in v:
                    var_parts.append(f"{v}='{name}-{env}'")
                elif "vpc" in v.lower():
                    var_parts.append(f"{v}=module.vpc.id")
                else:
                    var_parts.append(v)
            else:
                var_parts.append(v)

        var_str = ", ".join(var_parts) if var_parts else "no vars"

        # Co-deployed modules
        if env == "dev" and random.random() < 0.3:
            codeploy_str = "(standalone)"
        else:
            selected_siblings = random.sample(siblings, k=min(len(siblings), random.randint(1, len(siblings))))
            codeploy_str = f"co-deployed with: {', '.join(selected_siblings)}"

        # Tags
        tag_str = ""
        if tags_list:
            tag_dict = {
                "env": env,
                "project": name,
                "managed_by": "terraform",
            }
            for t in tags_list[:3]:
                tag_dict[t] = t
            tag_str = f" tags={{{', '.join(f'{k}={v}' for k, v in tag_dict.items())}}}"

        # Source locator
        locator = f"{consumer}@{''.join(random.choices('abcdef0123456789', k=7))}:{region}/{name}/main.tf"

        summary = (
            f"{repo}/{path}@{ver} in {env}/{region} as '{instance_name}' "
            f"with {var_str}; {codeploy_str}.{tag_str} "
            f"[{locator}]"
        )
        summaries.append(summary)

    return summaries


# -- Main test ---------------------------------------------------------------

async def main():
    from app.core.vector_store import make_session_factory
    from app.services.convention_distiller import (
        distill_all_dimensions, DIMENSIONS, MAX_TOKENS
    )
    from sqlalchemy import text

    print("=" * 70)
    print("CONVENTION DISTILLER — Round 6 Test")
    print(f"  MAX_TOKENS = {MAX_TOKENS}")
    print(f"  No char ceiling")
    print(f"  Dimensions: {', '.join(DIMENSIONS)}")
    print(f"  LLM: {os.environ.get('DESCRIPTION_LLM_MODEL', 'default')}")
    print("=" * 70)

    # Connect to DB and pick test modules
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            # Pick 3 modules with most variables (richest data)
            result = await db.execute(text("""
                SELECT DISTINCT ON (repo, module_path)
                    repo, module_name, module_path, version, tags,
                    variables, outputs, resources, description
                FROM modules
                WHERE variables != '{}' AND variables IS NOT NULL
                ORDER BY repo, module_path, indexed_at DESC
            """))
            all_modules = [dict(r) for r in result.mappings().all()]

        # Sort by variable count (richest first)
        all_modules.sort(
            key=lambda m: len(m.get("variables") or {}) if isinstance(m.get("variables"), dict) else 0,
            reverse=True,
        )

        # Pick 3 from different repos if possible
        test_modules = []
        seen_repos = set()
        for m in all_modules:
            if m["repo"] not in seen_repos or len(test_modules) < 3:
                test_modules.append(m)
                seen_repos.add(m["repo"])
                if len(test_modules) >= 3:
                    break

        if not test_modules:
            print("ERROR: No modules with variables found in the database.")
            return

        print(f"\nSelected {len(test_modules)} test modules:")
        for m in test_modules:
            var_count = len(m.get("variables", {})) if isinstance(m.get("variables"), dict) else 0
            print(f"  - {m['repo']}//{m['module_path']} ({var_count} vars, "
                  f"tags={m.get('tags', [])})")
        print()

    finally:
        await engine.dispose()

    # Run distillation for each module
    total_t0 = time.monotonic()
    all_results = {}

    for m in test_modules:
        module_ref = f"{m['repo']}/{m['module_path']}"
        print(f"\n{'-' * 70}")
        print(f"MODULE: {module_ref}")
        print(f"  vars: {len(m.get('variables', {})) if isinstance(m.get('variables'), dict) else 0}")
        print(f"  resources: {m.get('resources', [])[:5]}")
        print(f"  tags: {m.get('tags', [])}")
        print(f"{'-' * 70}")

        # Generate synthetic usages
        usages = generate_usage_summaries(m, count=15)
        print(f"\nGenerated {len(usages)} synthetic usage summaries.")
        print(f"Sample:\n  {usages[0][:150]}...\n")

        # Distill all dimensions
        t0 = time.monotonic()
        results = await distill_all_dimensions(module_ref, usages)
        elapsed = time.monotonic() - t0

        all_results[module_ref] = results

        print(f"\nResults ({elapsed:.1f}s total):")
        for r in results:
            dim = r["dimension"]
            assessment = r["assessment"]
            chars = r["char_count"]
            summary_preview = r["summary"][:200] if r["summary"] else "(empty)"

            status = "✓" if assessment in ("STRONG", "MODERATE") else "○" if assessment == "INSUFFICIENT_DATA" else "?"
            print(f"\n  {status} [{dim}] assessment={assessment} chars={chars}")
            print(f"    {summary_preview}{'...' if len(r.get('summary', '')) > 200 else ''}")

    total_elapsed = time.monotonic() - total_t0

    # Summary
    print(f"\n\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(f"Total time: {total_elapsed:.1f}s")
    print(f"Modules tested: {len(test_modules)}")

    for module_ref, results in all_results.items():
        print(f"\n  {module_ref}:")
        for r in results:
            chars = r["char_count"]
            assessment = r["assessment"]
            dim = r["dimension"]
            print(f"    {dim:12s}  {assessment:20s}  {chars:4d} chars")

    # Stats
    all_chars = [r["char_count"] for results in all_results.values() for r in results if r["char_count"] > 0]
    all_assessments = [r["assessment"] for results in all_results.values() for r in results]
    if all_chars:
        print(f"\nChar stats: min={min(all_chars)}, max={max(all_chars)}, "
              f"avg={sum(all_chars)/len(all_chars):.0f}")
    print(f"Assessments: {', '.join(f'{a}={all_assessments.count(a)}' for a in set(all_assessments))}")
    print(f"Max chars: {max(all_chars)}")


if __name__ == "__main__":
    asyncio.run(main())
