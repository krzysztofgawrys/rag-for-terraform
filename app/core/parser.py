import hcl2
import structlog
from pathlib import Path
from dataclasses import dataclass, field

log = structlog.get_logger()


@dataclass
class ParsedModule:
    repo: str
    module_name: str
    module_path: str
    version: str = "latest"
    tags: list[str] = field(default_factory=list)
    variables: dict = field(default_factory=dict)
    outputs: dict = field(default_factory=dict)
    resources: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)  # source paths
    raw_code: str = ""


def parse_repository(
    repo_dir: Path,
    repo_name: str,
    module_paths: list[str] | None = None,
) -> list[ParsedModule]:
    """
    Walk the entire repository and find all Terraform modules.
    Module = directory containing at least one .tf file

    module_paths: if set, only parse modules whose relative path is in this list.
    """
    modules = []
    module_dirs = _find_module_dirs(repo_dir)

    for module_dir in module_dirs:
        if module_paths is not None:
            rel = str(module_dir.relative_to(repo_dir))
            if rel not in module_paths:
                continue
        try:
            module = parse_module(module_dir, repo_name, repo_dir)
            modules.append(module)
            log.info("parsed_module", module=module.module_name, vars=len(module.variables))
        except Exception as e:
            log.warning("parse_failed", path=str(module_dir), error=str(e))

    return modules


def parse_module(module_dir: Path, repo_name: str, repo_root: Path) -> ParsedModule:
    variables: dict = {}
    outputs: dict = {}
    resources: list[str] = []
    dependencies: list[str] = []
    raw_parts: list[str] = []

    for tf_file in sorted(module_dir.glob("*.tf")):
        content = tf_file.read_text(encoding="utf-8", errors="replace")
        raw_parts.append(f"# === {tf_file.name} ===\n{content}")

        try:
            parsed = hcl2.loads(content)
        except Exception as e:
            log.warning("hcl_parse_error", file=str(tf_file), error=str(e))
            continue

        # Variables
        for var_block in parsed.get("variable", []):
            for name, cfg in var_block.items():
                variables[name] = {
                    "type": str(cfg.get("type", "any")),
                    "description": cfg.get("description", ""),
                    "default": cfg.get("default"),
                    "required": "default" not in cfg,
                }

        # Outputs
        for out_block in parsed.get("output", []):
            for name, cfg in out_block.items():
                outputs[name] = {
                    "description": cfg.get("description", ""),
                    "sensitive": cfg.get("sensitive", False),
                }

        # Resources
        for res_block in parsed.get("resource", []):
            for res_type in res_block.keys():
                if res_type not in resources:
                    resources.append(res_type)

        # Module dependencies (source = other modules)
        for mod_block in parsed.get("module", []):
            for _, cfg in mod_block.items():
                source = cfg.get("source", "")
                if source:
                    dependencies.append(_resolve_source(source, module_dir, repo_root))

    relative_path = str(module_dir.relative_to(repo_root))
    tags = _extract_tags(module_dir, relative_path, resources)

    return ParsedModule(
        repo=repo_name,
        module_name=module_dir.name,
        module_path=relative_path,
        tags=tags,
        variables=variables,
        outputs=outputs,
        resources=resources,
        dependencies=dependencies,
        raw_code="\n\n".join(raw_parts),
    )


# -- Helpers -------------------------------------------------------------------

def _find_module_dirs(repo_dir: Path) -> list[Path]:
    """
    A directory is a module if it contains .tf files and is NOT a subdirectory
    of another module (avoids duplicates for nested modules/).
    """
    tf_dirs = {p.parent for p in repo_dir.rglob("*.tf")}
    # Filter out .terraform directories (provider cache)
    return [
        d for d in sorted(tf_dirs)
        if ".terraform" not in d.parts
    ]


def _extract_tags(module_dir: Path, module_path: str,
                  resources: list[str]) -> list[str]:
    """
    Extract tags from multiple sources:
    1. Explicit files: tags.txt, .tags, TAGS
    2. locals.tf: locals { tags = [...] }
    3. Auto-generated from folder path segments
    4. Auto-generated from resource type names (aws_s3_bucket → s3)
    """
    tags = set()

    # 1. Explicit tag files
    for tag_file in ["tags.txt", ".tags", "TAGS"]:
        p = module_dir / tag_file
        if p.exists():
            tags.update(t.strip() for t in p.read_text().splitlines() if t.strip())

    # 2. locals.tf — look for locals block with key "tags"
    locals_tf = module_dir / "locals.tf"
    if locals_tf.exists():
        try:
            parsed = hcl2.loads(locals_tf.read_text())
            for loc_block in parsed.get("locals", []):
                raw_tags = loc_block.get("tags", {})
                if isinstance(raw_tags, dict):
                    tags.update(raw_tags.values())
                elif isinstance(raw_tags, list):
                    tags.update(raw_tags)
        except Exception:
            pass

    # 3. Auto-tag from path segments (e.g. "waf/waf_global_block_aml_acl" → waf)
    _SKIP_FOLDERS = {"modules", "module", "terraform", "templates", "products"}
    path_parts = module_path.replace("\\", "/").split("/")
    for part in path_parts[:-1]:  # folders only, skip module dir name
        cleaned = part.strip().lower()
        if cleaned and cleaned not in _SKIP_FOLDERS:
            tags.add(cleaned)

    # 4. Auto-tag from resource types
    #    "aws_s3_bucket" → "s3", "aws_lambda_function" → "lambda"
    _CLOUD_PREFIXES = {"aws", "azurerm", "google"}
    for res in resources:
        parts = res.split("_")
        if len(parts) >= 3 and parts[0] in _CLOUD_PREFIXES:
            service = parts[1]
            if service and len(service) > 1:
                tags.add(service)

    return sorted(tags)


def _resolve_source(source: str, module_dir: Path, repo_root: Path) -> str:
    """Resolve relative path to a path relative to repo root."""
    if source.startswith("./") or source.startswith("../"):
        resolved = (module_dir / source).resolve()
        try:
            return str(resolved.relative_to(repo_root.resolve()))
        except ValueError:
            return source
    return source  # registry or git URL — leave as-is (parsed by graph.py)
