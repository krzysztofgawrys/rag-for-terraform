"""Golden end-to-end test for app.core.consumer_parser.parse_consumer_repo.

Parses a real consumer-repo fixture tree and asserts the full ParsedUsage
list - the last untested piece of the consumer-parsing pipeline. This is what
feeds build_usage_summary / build_compose_summary (already tested in isolation),
so locking the assembly closes the loop from .tf-on-disk to embedded snippet.

Exercises behaviours only visible end-to-end:
  - recursive *.tf walk in sorted order, .terraform/ skipped
  - sibling cross-referencing between module blocks in the same file
  - env/region overridden by a module variable, falling back to the path hint
  - source_locator format (commit_sha truncated to 7 chars)

NOTE: the ParsedUsage docstring advertises a ':Lstart-Lend' line range in
source_locator, but _parse_file does NOT emit one. This test pins the ACTUAL
format (no line numbers); if line ranges are ever implemented, this is the
assertion to update.

Pinned to python-hcl2 4.3.2 (see test_parser_golden.py for why the pin matters).

Run:  pytest tests/test_consumer_repo_golden.py -v
"""
from pathlib import Path

import pytest

from app.core.consumer_parser import parse_consumer_repo

FIXTURE = Path(__file__).parent / "fixtures" / "consumer"
SHA = "abc1234def5678"   # -> truncated to 'abc1234' in source_locator


@pytest.fixture(scope="module")
def usages():
    return parse_consumer_repo(str(FIXTURE), "infra-live", commit_sha=SHA)


@pytest.fixture(scope="module")
def by_instance(usages):
    return {u.instance_name: u for u in usages}


# --------------------------------------------------------------------------
# walk / discovery
# --------------------------------------------------------------------------
def test_three_usages_terraform_dir_skipped(usages):
    # the module inside .terraform/ must be ignored entirely
    assert len(usages) == 3
    assert {u.instance_name for u in usages} == {"vpc", "bucket", "key"}
    assert all(u.module_ref != "tf-modules/should-not-appear" for u in usages)


def test_usage_order_follows_sorted_files_then_blocks(usages):
    # sorted rglob: 'dev/single.tf' < 'prod/eu-west-1/stack.tf';
    # within stack.tf, block order is bucket then key
    assert [u.instance_name for u in usages] == ["vpc", "bucket", "key"]


# --------------------------------------------------------------------------
# resolution + version + vars
# --------------------------------------------------------------------------
def test_bucket_full_shape(by_instance):
    u = by_instance["bucket"]
    assert u.module_ref == "tf-modules/s3"
    assert u.version_ref == "v1.0.0"
    assert u.var_keys == ["bucket_name", "versioning"]
    assert u.var_literals == {"bucket_name": "prod-data", "versioning": "True"}
    assert u.consumer_path == "prod/eu-west-1/stack.tf"


def test_key_full_shape(by_instance):
    u = by_instance["key"]
    assert u.module_ref == "tf-modules/kms"
    assert u.version_ref == "v2.1.0"
    assert u.var_literals == {"alias": "prod-key"}


def test_nested_module_path_resolves(by_instance):
    # //networking/vpc keeps the sub-path under the repo name
    assert by_instance["vpc"].module_ref == "tf-modules/networking/vpc"
    assert by_instance["vpc"].version_ref == "v3.0.0"


# --------------------------------------------------------------------------
# siblings (co-deploy detection within a file)
# --------------------------------------------------------------------------
def test_siblings_cross_reference_within_file(by_instance):
    # bucket and key share stack.tf -> each is the other's sibling, self excluded
    assert by_instance["bucket"].siblings == ["tf-modules/kms"]
    assert by_instance["key"].siblings == ["tf-modules/s3"]


def test_lone_module_has_no_siblings(by_instance):
    assert by_instance["vpc"].siblings == []


# --------------------------------------------------------------------------
# env / region: path hint vs variable override
# --------------------------------------------------------------------------
def test_env_region_from_path(by_instance):
    u = by_instance["bucket"]
    assert u.env == "prod"            # from 'prod/...' path segment
    assert u.region == "eu-west-1"    # from path segment


def test_env_overridden_by_module_variable(by_instance):
    """vpc lives under dev/ (hint 'dev') but passes environment = "qa".
    The explicit variable must win over the path heuristic."""
    u = by_instance["vpc"]
    assert u.env == "qa"              # variable beats the 'dev' path hint
    assert u.region == ""             # no region anywhere -> empty


# --------------------------------------------------------------------------
# source_locator (ACTUAL format - no line range)
# --------------------------------------------------------------------------
def test_source_locator_truncates_sha_and_has_no_line_range(by_instance):
    assert by_instance["bucket"].source_locator == "infra-live@abc1234:prod/eu-west-1/stack.tf"
    assert by_instance["vpc"].source_locator == "infra-live@abc1234:dev/single.tf"


def test_source_locator_without_commit_sha():
    """No commit_sha -> no '@...' segment at all."""
    usages = parse_consumer_repo(str(FIXTURE), "infra-live")  # sha=""
    loc = {u.instance_name: u.source_locator for u in usages}
    assert loc["vpc"] == "infra-live:dev/single.tf"
