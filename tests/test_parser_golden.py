"""Golden test for app.core.parser.parse_module / parse_repository.

Parses real .tf fixtures under tests/fixtures/repo and asserts the full
ParsedModule shape. This locks in two things at once:

  1. Your extraction logic (variables/outputs/resources/tags/dependencies).
  2. The behaviour of the pinned python-hcl2 (==4.3.2 in requirements.txt).

WHY THE PIN MATTERS — verified, not theoretical:
hcl2 4.3.2 returns string literals and block labels UNQUOTED ('aws_s3_bucket',
'bucket_name'). Newer python-hcl2 (>=5) embeds the surrounding quotes in every
value ('"aws_s3_bucket"', '"bucket_name"'), which silently corrupts variable
names, resource types, tags, AND breaks _resolve_source relative-path handling
(a quoted '"../s3"' no longer starts with '../', so the dependency edge is
never resolved and the dependency graph goes wrong). If someone bumps hcl2
without re-checking, THIS test is what catches it.

Run:  pytest tests/test_parser_golden.py -v
"""
from pathlib import Path

import pytest

from app.core.parser import parse_repository

FIXTURE_REPO = Path(__file__).parent / "fixtures" / "repo"


@pytest.fixture(scope="module")
def modules():
    parsed = parse_repository(FIXTURE_REPO, "fixture-repo")
    return {m.module_path: m for m in parsed}


def test_discovers_both_modules(modules):
    assert set(modules) == {"modules/s3", "modules/app"}


# --------------------------------------------------------------------------
# s3 module — the data-rich one
# --------------------------------------------------------------------------
def test_s3_identity(modules):
    m = modules["modules/s3"]
    assert m.repo == "fixture-repo"
    assert m.module_name == "s3"
    assert m.module_path == "modules/s3"


def test_s3_resources_deduped_and_ordered(modules):
    # collected across sorted *.tf files, first-seen order, no quotes (hcl2 4.3.2)
    assert modules["modules/s3"].resources == ["aws_s3_bucket", "aws_s3_bucket_versioning"]


def test_s3_tags(modules):
    # 's3' from resource service names + 'storage'/'s3' from locals.tf list,
    # 'modules' path segment is in the skip set -> sorted, deduped
    assert modules["modules/s3"].tags == ["s3", "storage"]


def test_s3_variable_required_flags(modules):
    """`required` is derived purely from absence of a default — the single
    most consequential per-variable fact for downstream HCL composition."""
    v = modules["modules/s3"].variables
    assert set(v) == {"bucket_name", "versioning", "tags"}
    assert v["bucket_name"]["required"] is True          # no default
    assert v["versioning"]["required"] is False           # default = true
    assert v["tags"]["required"] is False                 # default = {}


def test_s3_variable_defaults_and_descriptions(modules):
    v = modules["modules/s3"].variables
    assert v["bucket_name"]["default"] is None
    assert v["bucket_name"]["description"] == "Name of the bucket"
    assert v["versioning"]["default"] is True
    assert v["tags"]["default"] == {}


def test_s3_variable_types_lock_hcl2_representation(modules):
    """hcl2 4.3.2 renders type expressions as '${...}'. Pinning these values
    means a hcl2 upgrade that changes the representation fails loudly here."""
    v = modules["modules/s3"].variables
    assert v["bucket_name"]["type"] == "${string}"
    assert v["versioning"]["type"] == "${bool}"
    assert v["tags"]["type"] == "${map(string)}"


def test_s3_outputs_and_sensitive_flag(modules):
    o = modules["modules/s3"].outputs
    assert set(o) == {"bucket_arn", "bucket_id"}
    assert o["bucket_arn"]["description"] == "ARN of the bucket"
    assert o["bucket_arn"]["sensitive"] is False
    assert o["bucket_id"]["sensitive"] is True            # explicitly sensitive


def test_s3_has_no_dependencies(modules):
    assert modules["modules/s3"].dependencies == []


# --------------------------------------------------------------------------
# app module — dependency resolution + tag edge case
# --------------------------------------------------------------------------
def test_app_relative_dependency_resolved_to_repo_path(modules):
    """source = "../s3" must resolve to the repo-relative path "modules/s3",
    not be left as "../s3". This is what feeds the dependency graph; if it
    regresses, graph edges silently fail to match."""
    assert modules["modules/app"].dependencies == ["modules/s3"]


def test_app_two_part_resource_yields_no_service_tag(modules):
    """'aws_instance' has only 2 underscore-parts, so the service-name tag
    heuristic (needs >=3 parts: cloud_service_kind) must NOT fire. Combined
    with the skipped 'modules' path segment, app ends up with zero tags."""
    m = modules["modules/app"]
    assert m.resources == ["aws_instance"]
    assert m.tags == []
