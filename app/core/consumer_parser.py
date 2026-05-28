"""
Consumer repo parser — extracts module {} blocks from Terraform consumer repos.

Parses each .tf file for `module "name" { source = "..." }` blocks and extracts:
  - instance name
  - source URL → resolved module_ref
  - version ref
  - variable keys + literal values (no complex expressions)
  - sibling modules in the same file (for co-deploy detection)
  - environment / region hints from path

Does NOT copy raw HCL — only extracts structured signals for summary generation.
"""

import os
import re
import structlog
import hcl2
import json

from dataclasses import dataclass, field
from pathlib import Path

log = structlog.get_logger()


@dataclass
class ParsedUsage:
    """One module {} block from a consumer repo."""
    instance_name: str              # module "this_name" { ... }
    source_url: str                 # raw source value
    module_ref: str                 # resolved: 'repo/path'
    version_ref: str                # extracted ?ref=vX.Y.Z or ""
    var_keys: list[str]             # variable names passed
    var_literals: dict[str, str]    # var_name → literal value (strings/bools/numbers only)
    siblings: list[str]             # other module_refs in the same file
    env: str                        # guessed from path or var
    region: str                     # guessed from path or var
    consumer_repo: str
    consumer_path: str              # relative file path
    source_locator: str             # 'consumer-repo@sha:path/file.tf:Lstart-Lend'


def parse_consumer_repo(
    repo_dir: str,
    consumer_repo: str,
    commit_sha: str = "",
) -> list[ParsedUsage]:
    """Walk a consumer repo directory and extract all module {} usages."""
    repo_path = Path(repo_dir)
    all_usages = []

    for tf_file in sorted(repo_path.rglob("*.tf")):
        # Skip .terraform directories
        if ".terraform" in tf_file.parts:
            continue

        rel_path = str(tf_file.relative_to(repo_path))
        try:
            usages = _parse_file(tf_file, rel_path, consumer_repo, commit_sha)
            all_usages.extend(usages)
        except Exception as e:
            log.warning("consumer_parse_error", file=rel_path, error=str(e))

    log.info("consumer_repo_parsed", repo=consumer_repo, usages=len(all_usages))
    return all_usages


def _parse_file(
    tf_file: Path,
    rel_path: str,
    consumer_repo: str,
    commit_sha: str,
) -> list[ParsedUsage]:
    """Parse a single .tf file and return ParsedUsage for each module {} block."""
    try:
        with open(tf_file, "r") as f:
            parsed = hcl2.load(f)
    except Exception as e:
        log.warning("hcl_parse_error", file=rel_path, error=str(e))
        return []

    module_blocks = parsed.get("module", [])
    if not module_blocks:
        return []

    # First pass: collect all module_refs in this file (for siblings)
    all_refs = []
    blocks_data = []
    for block in module_blocks:
        for instance_name, body in block.items():
            source = body.get("source", "")
            if not source:
                continue
            ref = _resolve_source(source)
            version_ref = _extract_version_ref(source, body)
            all_refs.append(ref)
            blocks_data.append((instance_name, body, source, ref, version_ref))

    # Second pass: build ParsedUsage with siblings
    usages = []
    env_hint = _guess_env(rel_path)
    region_hint = _guess_region(rel_path)

    for instance_name, body, source, module_ref, version_ref in blocks_data:
        if not module_ref:
            continue

        # Siblings = other module_refs in the same file (excluding self)
        siblings = [r for r in all_refs if r != module_ref and r]

        # Extract variable keys and literal values
        var_keys, var_literals = _extract_vars(body)

        # Override env/region from variables if available
        env = var_literals.get("env", var_literals.get("environment", env_hint))
        region = var_literals.get("region", var_literals.get("aws_region", region_hint))

        sha_part = f"@{commit_sha[:7]}" if commit_sha else ""
        source_locator = f"{consumer_repo}{sha_part}:{rel_path}"

        usages.append(ParsedUsage(
            instance_name=instance_name,
            source_url=source,
            module_ref=module_ref,
            version_ref=version_ref,
            var_keys=var_keys,
            var_literals=var_literals,
            siblings=siblings,
            env=env,
            region=region,
            consumer_repo=consumer_repo,
            consumer_path=rel_path,
            source_locator=source_locator,
        ))

    return usages


def _resolve_source(source: str) -> str:
    """Resolve a Terraform source URL to a module_ref like 'repo/path'.

    Handles:
      - git::ssh://git@github.com/org/repo.git//path?ref=v1.0
      - git@github.com:org/repo.git//path?ref=v1.0
      - github.com/org/repo//path
      - ../relative/path (returns empty — can't resolve without context)
    """
    if source.startswith("./") or source.startswith("../"):
        return ""  # relative modules - skip

    # Terraform Registry format: org/name/provider//subpath
    # e.g. "terraform-aws-modules/ecs/aws//modules/cluster" -> "terraform-aws-ecs/modules/cluster"
    registry_m = re.match(r"^([^/]+)/([^/]+)/([^/]+?)(?://(.*))?$", source)
    if registry_m and not source.startswith("git") and ":" not in source and "." not in registry_m.group(1):
        org, name, provider = registry_m.group(1), registry_m.group(2), registry_m.group(3)
        subpath = registry_m.group(4) or ""
        repo_name = f"terraform-{provider}-{name}"
        return f"{repo_name}/{subpath}" if subpath else repo_name

    # Strip git:: prefix
    clean = re.sub(r'^git::', '', source)
    # Strip ssh:// prefix
    clean = re.sub(r'^ssh://', '', clean)
    # Strip ?ref=... suffix
    clean = re.sub(r'\?ref=.*$', '', clean)

    # git@github.com:org/repo.git//path → org/repo//path
    m = re.match(r'git@[^:]+:(.+?)(?:\.git)?(?://(.*))?$', clean)
    if m:
        org_repo = m.group(1)  # 'org/repo'
        path = m.group(2) or ""
        repo_name = org_repo.split("/")[-1] if "/" in org_repo else org_repo
        return f"{repo_name}/{path}" if path else repo_name

    # github.com/org/repo//path or https://github.com/org/repo//path
    clean = re.sub(r'^https?://', '', clean)
    m = re.match(r'[^/]+/[^/]+/([^/]+?)(?:\.git)?(?://(.*))?$', clean)
    if m:
        repo_name = m.group(1)
        path = m.group(2) or ""
        return f"{repo_name}/{path}" if path else repo_name

    return ""


_BRANCH_REF_RE = re.compile(r'^(master|main|develop|trunk|HEAD)$', re.IGNORECASE)


def _extract_version_ref(source: str, body: dict) -> str:
    """Extract version ref from source URL or version attribute.

    Returns "" for branch refs (main/master/develop/HEAD) — those are
    "rolling" deployments, not pinned versions, and should not feed into
    convention distillation's `versions` dimension which asserts "all
    deployments use exact semver pinning".
    """
    # ?ref=vX.Y.Z in source
    m = re.search(r'\?ref=([^\s&"]+)', source)
    if m:
        ref = m.group(1)
        return "" if _BRANCH_REF_RE.match(ref) else ref

    # version = "~> 1.0" or version = "1.0.0"
    version = body.get("version", "")
    if version:
        v = str(version).strip()
        return "" if _BRANCH_REF_RE.match(v) else v

    return ""


def _extract_vars(body: dict) -> tuple[list[str], dict[str, str]]:
    """Extract variable keys and literal values from a module body.

    Returns (all_keys, literal_values).
    Literal values: strings, bools, numbers only.
    Complex expressions (references, function calls) → key only, no value.
    """
    skip_keys = {"source", "version", "providers", "depends_on", "count", "for_each"}
    var_keys = []
    var_literals = {}

    for key, value in body.items():
        if key in skip_keys:
            continue
        var_keys.append(key)

        if isinstance(value, str):
            # Skip Terraform references/expressions
            if "${" in value or "module." in value or "var." in value or "data." in value:
                continue
            var_literals[key] = value
        elif isinstance(value, bool):
            var_literals[key] = str(value)
        elif isinstance(value, (int, float)):
            var_literals[key] = str(value)
        # Lists, dicts, complex types → key only

    return var_keys, var_literals


def _guess_env(path: str) -> str:
    """Guess environment from path segments."""
    path_lower = path.lower()
    for env in ("prod", "production", "staging", "stg", "dev", "development", "test", "uat"):
        if env in path_lower.split("/") or env in path_lower.split("-") or env in path_lower.split("_"):
            if env in ("production",):
                return "prod"
            if env in ("staging", "stg"):
                return "staging"
            if env in ("development",):
                return "dev"
            return env
    return ""


def _guess_region(path: str) -> str:
    """Guess AWS region from path segments."""
    m = re.search(r'(eu-west-\d|us-east-\d|us-west-\d|ap-southeast-\d|ap-northeast-\d)', path)
    return m.group(1) if m else ""


def build_compose_summary(usages: list[ParsedUsage]) -> str | None:
    """Build a stack-level summary for a single .tf file with multiple module
    calls. Returns None when fewer than 2 modules are present in the file.

    The summary describes the file as a "compose pattern" — a real-world
    example of how the organisation wires multiple indexed modules into a
    cohesive stack. Used by the retriever to surface high-quality reference
    points for "build me a full X" queries.
    """
    if len(usages) < 2:
        return None

    # All usages share consumer_repo / consumer_path / siblings — sanity guard
    first = usages[0]
    file_path = first.consumer_path
    consumer_repo = first.consumer_repo

    # Deduplicate module_refs preserving order
    seen = set()
    ordered_refs: list[str] = []
    for u in usages:
        if u.module_ref and u.module_ref not in seen:
            ordered_refs.append(u.module_ref)
            seen.add(u.module_ref)

    # Instance names in order
    instances = ", ".join(f'module.{u.instance_name}' for u in usages[:20])

    # Versions used (per module_ref, first seen)
    version_by_ref: dict[str, str] = {}
    for u in usages:
        if u.module_ref and u.module_ref not in version_by_ref and u.version_ref:
            version_by_ref[u.module_ref] = u.version_ref

    version_lines: list[str] = []
    for ref in ordered_refs[:12]:
        v = version_by_ref.get(ref, "")
        version_lines.append(f"  - {ref}" + (f"@{v}" if v else ""))

    env_region = "/".join(filter(None, [first.env, first.region]))
    location = f" in {env_region}" if env_region else ""

    summary = (
        f"Compose pattern: {file_path} (consumer repo: {consumer_repo}{location})\n"
        f"Wires {len(usages)} module calls — {len(ordered_refs)} distinct modules.\n"
        f"Instances: {instances}\n"
        f"Modules used:\n" + "\n".join(version_lines)
    )
    return summary


def build_usage_summary(usage: ParsedUsage) -> str:
    """Build a natural language summary of a single module usage.

    This summary is what gets embedded and stored in knowledge_snippets.
    """
    parts = [f"{usage.module_ref}"]

    if usage.version_ref:
        parts[0] += f"@{usage.version_ref}"

    if usage.env or usage.region:
        location = "/".join(filter(None, [usage.env, usage.region]))
        parts.append(f"in {location}")

    parts.append(f"as '{usage.instance_name}'")

    # Variables: show literals first, then keys-only
    if usage.var_literals:
        lit_parts = [f"{k}='{v}'" for k, v in list(usage.var_literals.items())[:8]]
        remaining_keys = [k for k in usage.var_keys if k not in usage.var_literals]
        if remaining_keys:
            lit_parts.extend(remaining_keys[:6])
        parts.append(f"with {', '.join(lit_parts)}")
    elif usage.var_keys:
        parts.append(f"with {', '.join(usage.var_keys[:10])}")

    # Co-deployment
    if usage.siblings:
        parts.append(f"co-deployed with: {', '.join(usage.siblings[:5])}")
    else:
        parts.append("(standalone)")

    summary = " ".join(parts)

    # Source locator at the end
    summary += f" [{usage.source_locator}]"

    return summary
