"""Tests for the remaining pure functions in app.core.consumer_parser.

These produce the exact text that gets embedded into knowledge_snippets and
fed to the convention distiller. A format change here silently corrupts the
"authoritative" layer at its source - before the distiller ever sees it - so
the summary builders are asserted as golden strings.

Covers:
  - _extract_vars        body dict -> (var_keys, literal_values)
  - _guess_env           path -> normalised environment
  - _guess_region        path -> AWS region
  - build_usage_summary  ParsedUsage -> embedded snippet text  (GOLDEN)
  - build_compose_summary [ParsedUsage] -> stack snippet text   (GOLDEN)

Run:  pytest tests/test_consumer_summaries.py -v
"""
import pytest

from app.core.consumer_parser import (
    _extract_vars,
    _guess_env,
    _guess_region,
    build_usage_summary,
    build_compose_summary,
    ParsedUsage,
)


# --------------------------------------------------------------------------
# _extract_vars
# --------------------------------------------------------------------------
def test_extract_vars_separates_literals_from_keys():
    body = {
        "source": "git::...",          # skip
        "version": "v1",                # skip
        "count": 3,                     # skip (meta-arg)
        "name": "my-bucket",            # literal string
        "enabled": True,                # bool -> "True"
        "replicas": 2,                  # number -> "2"
        "ref_val": "${var.x}",          # expression -> key only
        "mod_ref": "module.foo.id",     # reference -> key only
        "tags": {"a": "b"},             # dict -> key only
        "subnets": ["a", "b"],          # list -> key only
    }
    keys, literals = _extract_vars(body)
    assert keys == ["name", "enabled", "replicas", "ref_val", "mod_ref", "tags", "subnets"]
    assert literals == {"name": "my-bucket", "enabled": "True", "replicas": "2"}


def test_extract_vars_skips_meta_arguments():
    """source/version/providers/depends_on/count/for_each must never appear as
    user variables - they are Terraform meta-arguments, not module inputs."""
    body = {k: "x" for k in
            ("source", "version", "providers", "depends_on", "count", "for_each")}
    body["real_input"] = "y"
    keys, literals = _extract_vars(body)
    assert keys == ["real_input"]
    assert literals == {"real_input": "y"}


def test_extract_vars_expression_kept_as_key_without_value():
    keys, literals = _extract_vars({"bucket": "data.aws_s3_bucket.x.id"})
    assert keys == ["bucket"]
    assert "bucket" not in literals          # reference -> no literal value


# --------------------------------------------------------------------------
# _guess_env  (exact segment membership, not substring)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path,expected", [
    ("infra/prod/vpc", "prod"),
    ("envs/production/app", "prod"),         # production -> prod
    ("staging-eu", "staging"),
    ("stg", "staging"),                       # stg -> staging
    ("app/dev/x", "dev"),
    ("development/x", "dev"),                  # development -> dev
    ("ci/test/x", "test"),
    ("uat/x", "uat"),
    ("products/vpc", ""),                     # MUST NOT substring-match 'prod'
    ("foo/bar", ""),
])
def test_guess_env(path, expected):
    assert _guess_env(path) == expected


def test_guess_env_products_is_not_prod():
    """Regression guard for the classic substring trap: 'products' contains
    'prod' but is not a production environment. Matching is by path segment,
    not substring - if this regresses, every products/ path is mislabelled."""
    assert _guess_env("catalog/products/listing") == ""


# --------------------------------------------------------------------------
# _guess_region
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path,expected", [
    ("infra/eu-west-1/vpc", "eu-west-1"),
    ("x/us-east-2/y", "us-east-2"),
    ("ap-southeast-1/z", "ap-southeast-1"),
    ("frankfurt/z", ""),                      # non-region word
    ("no/region/here", ""),
])
def test_guess_region(path, expected):
    assert _guess_region(path) == expected


# --------------------------------------------------------------------------
# build_usage_summary  (GOLDEN - this string is what gets embedded)
# --------------------------------------------------------------------------
def _usage(**kw):
    base = dict(
        instance_name="this", source_url="src", module_ref="mods/s3",
        version_ref="", var_keys=[], var_literals={}, siblings=[],
        env="", region="", consumer_repo="infra", consumer_path="prod/s3.tf",
        source_locator="infra@abc123:prod/s3.tf:L1-L9",
    )
    base.update(kw)
    return ParsedUsage(**base)


def test_usage_summary_full():
    u = _usage(
        version_ref="v1.2.0", env="prod", region="eu-west-1",
        var_keys=["bucket_name", "versioning"],
        var_literals={"bucket_name": "data-bucket"},
        siblings=["mods/kms", "mods/iam"],
    )
    assert build_usage_summary(u) == (
        "mods/s3@v1.2.0 in prod/eu-west-1 as 'this' "
        "with bucket_name='data-bucket', versioning "
        "co-deployed with: mods/kms, mods/iam "
        "[infra@abc123:prod/s3.tf:L1-L9]"
    )


def test_usage_summary_keys_only_standalone():
    u = _usage(var_keys=["a", "b", "c"])
    assert build_usage_summary(u) == (
        "mods/s3 as 'this' with a, b, c (standalone) "
        "[infra@abc123:prod/s3.tf:L1-L9]"
    )


def test_usage_summary_minimal_standalone():
    assert build_usage_summary(_usage()) == (
        "mods/s3 as 'this' (standalone) [infra@abc123:prod/s3.tf:L1-L9]"
    )


def test_usage_summary_literals_capped_at_eight():
    """Only the first 8 literal values are inlined - guards prompt-bloat."""
    lits = {f"k{i}": f"v{i}" for i in range(12)}
    u = _usage(var_keys=list(lits), var_literals=lits)
    s = build_usage_summary(u)
    assert "k7='v7'" in s
    assert "k8='v8'" not in s                 # 9th literal dropped


# --------------------------------------------------------------------------
# build_compose_summary  (GOLDEN - stack-pattern snippet)
# --------------------------------------------------------------------------
def test_compose_summary_two_modules():
    common = dict(consumer_path="prod/stack.tf", env="prod", region="eu-west-1")
    u1 = _usage(instance_name="bucket", module_ref="mods/s3", version_ref="v1.0", **common)
    u2 = _usage(instance_name="key", module_ref="mods/kms", version_ref="v2.0", **common)
    assert build_compose_summary([u1, u2]) == (
        "Compose pattern: prod/stack.tf (consumer repo: infra in prod/eu-west-1)\n"
        "Wires 2 module calls - 2 distinct modules.\n"
        "Instances: module.bucket, module.key\n"
        "Modules used:\n"
        "  - mods/s3@v1.0\n"
        "  - mods/kms@v2.0"
    )


def test_compose_summary_requires_two_modules():
    """A single module call is not a 'compose pattern' - must return None so it
    never becomes a stack snippet."""
    assert build_compose_summary([_usage()]) is None
    assert build_compose_summary([]) is None
