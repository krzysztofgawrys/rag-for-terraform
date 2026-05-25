#!/usr/bin/env python3
"""
Quick test for convention_distiller.py — Round 6.
1 module, all 6 dimensions, 10 usages. Designed for fast feedback.
"""

import asyncio
import os
import sys
import time
import random

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENVS = ["prod", "staging", "dev"]
REGIONS = ["eu-west-1", "us-east-1"]
REGION_SHORT = {"eu-west-1": "eu", "us-east-1": "us"}


def generate_usage_summaries(module: dict, count: int = 10) -> list[str]:
    name = module["module_name"]
    repo = module["repo"]
    path = module["module_path"]
    variables = module.get("variables") or {}
    resources = module.get("resources") or []
    tags_list = module.get("tags") or []

    var_names = list(variables.keys()) if isinstance(variables, dict) else []
    required_vars = [
        v for v, cfg in variables.items()
        if isinstance(cfg, dict) and cfg.get("required", False)
    ] if isinstance(variables, dict) else []
    optional_vars = [v for v in var_names if v not in required_vars]

    versions = ["v2.1.0", "v2.0.0", "v1.9.0"]
    siblings = ["shared/vpc", "core/alb", "core/certificate"]

    summaries = []
    for i in range(count):
        env = random.choices(ENVS, weights=[0.5, 0.3, 0.2])[0]
        region = random.choice(REGIONS)
        region_short = REGION_SHORT[region]
        ver = random.choice(versions)

        instance_name = f"{name}-{env}-{region_short}"

        passed_vars = list(required_vars[:6])
        for ov in optional_vars[:4]:
            if random.random() < 0.4:
                passed_vars.append(ov)

        var_parts = []
        for v in passed_vars[:6]:
            cfg = variables.get(v, {})
            if isinstance(cfg, dict):
                default = cfg.get("default")
                if default is not None and not isinstance(default, (dict, list)):
                    var_parts.append(f"{v}='{default}'")
                elif v in ("env", "environment"):
                    var_parts.append(f"{v}='{env}'")
                elif "name" in v:
                    var_parts.append(f"{v}='{name}-{env}'")
                else:
                    var_parts.append(v)
            else:
                var_parts.append(v)

        var_str = ", ".join(var_parts) if var_parts else "no vars"

        if env == "dev" and random.random() < 0.3:
            codeploy_str = "(standalone)"
        else:
            sel = random.sample(siblings, k=random.randint(1, len(siblings)))
            codeploy_str = f"co-deployed with: {', '.join(sel)}"

        tag_dict = {"env": env, "project": name, "managed_by": "terraform"}
        tag_str = f" tags={{{', '.join(f'{k}={v}' for k, v in tag_dict.items())}}}"

        consumer = f"tf-infra-{env}"
        locator = f"{consumer}@{''.join(random.choices('abcdef0123456789', k=7))}:{region}/{name}/main.tf"

        summary = (
            f"{repo}/{path}@{ver} in {env}/{region} as '{instance_name}' "
            f"with {var_str}; {codeploy_str}.{tag_str} [{locator}]"
        )
        summaries.append(summary)

    return summaries


async def main():
    from app.core.vector_store import make_session_factory
    from app.services.convention_distiller import (
        distill_all_dimensions, distill_dimension, DIMENSIONS, MAX_TOKENS
    )
    from sqlalchemy import text

    print("=" * 70)
    print("CONVENTION DISTILLER — Round 6 Quick Test")
    print(f"  MAX_TOKENS = {MAX_TOKENS}")
    print(f"  No char ceiling")
    print(f"  Dimensions: {', '.join(DIMENSIONS)}")
    print("=" * 70)

    # Pick 1 module with moderate variable count
    engine, SessionLocal = make_session_factory()
    try:
        async with SessionLocal() as db:
            result = await db.execute(text("""
                SELECT repo, module_name, module_path, version, tags,
                    variables, outputs, resources, description
                FROM modules
                WHERE repo = 'terraform-infrastructure-services'
                  AND version = 'master'
                  AND module_path NOT LIKE '%example%'
                  AND module_path NOT LIKE '%wrapper%'
                ORDER BY jsonb_array_length(
                    COALESCE(
                        (SELECT jsonb_agg(k) FROM jsonb_object_keys(COALESCE(variables, '{}'::jsonb)) AS k),
                        '[]'::jsonb
                    )
                ) DESC
                LIMIT 1
            """))
            row = result.mappings().first()
            if not row:
                print("ERROR: No suitable module found")
                return
            module = dict(row)
    finally:
        await engine.dispose()

    module_ref = f"{module['repo']}/{module['module_path']}"
    var_count = len(module.get("variables", {})) if isinstance(module.get("variables"), dict) else 0

    print(f"\nTest module: {module_ref}")
    print(f"  vars: {var_count}")
    print(f"  resources: {module.get('resources', [])[:5]}")
    print(f"  tags: {module.get('tags', [])}")

    # Generate usages
    usages = generate_usage_summaries(module, count=10)
    print(f"\nGenerated {len(usages)} usage summaries.")
    print(f"\nSample usages:")
    for u in usages[:3]:
        print(f"  {u[:120]}...")

    # Run each dimension one at a time, printing results immediately
    print(f"\n{'=' * 70}")
    print("DISTILLATION RESULTS")
    print(f"{'=' * 70}")

    total_t0 = time.monotonic()
    results = []

    for dim in DIMENSIONS:
        print(f"\n--- {dim.upper()} ---")
        t0 = time.monotonic()
        r = await distill_dimension(module_ref, dim, usages)
        elapsed = time.monotonic() - t0

        if r:
            results.append(r)
            print(f"  Assessment: {r['assessment']}")
            print(f"  Chars: {r['char_count']}")
            print(f"  Time: {elapsed:.1f}s")
            print(f"  Content:")
            # Print full content, wrapped
            lines = r["summary"].split("\n")
            for line in lines:
                while len(line) > 100:
                    print(f"    {line[:100]}")
                    line = line[100:]
                print(f"    {line}")
        else:
            print(f"  FAILED (no response)")
            print(f"  Time: {elapsed:.1f}s")

    total_elapsed = time.monotonic() - total_t0

    # Summary
    print(f"\n{'=' * 70}")
    print("SUMMARY")
    print(f"{'=' * 70}")
    print(f"Module: {module_ref}")
    print(f"Total time: {total_elapsed:.1f}s ({total_elapsed/len(DIMENSIONS):.1f}s avg per dimension)")
    print()

    for r in results:
        print(f"  {r['dimension']:12s}  {r['assessment']:20s}  {r['char_count']:4d} chars")

    all_chars = [r["char_count"] for r in results if r["char_count"] > 0]
    if all_chars:
        print(f"\nChar stats: min={min(all_chars)}, max={max(all_chars)}, "
              f"avg={sum(all_chars)/len(all_chars):.0f}")

    all_assessments = [r["assessment"] for r in results]
    print(f"Assessments: {', '.join(f'{a}={all_assessments.count(a)}' for a in set(all_assessments))}")


if __name__ == "__main__":
    asyncio.run(main())
