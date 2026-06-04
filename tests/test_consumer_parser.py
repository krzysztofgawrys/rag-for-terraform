"""Unit tests for app.core.consumer_parser source/version resolution.

These cover the pure, deterministic regex functions whose silent breakage
corrupts the "authoritative" convention data downstream:

  - _resolve_source:      source URL -> "repo/path" module_ref
  - _extract_version_ref: source/version -> pinned ref ("" for branch refs)

Run:  pytest tests/test_consumer_parser.py -v
"""

import pytest

from app.core.consumer_parser import _resolve_source, _extract_version_ref


# --------------------------------------------------------------------------
# _resolve_source
# --------------------------------------------------------------------------
# Each row: (source_url, expected_module_ref)
RESOLVE_CASES = [
    # --- relative paths: unresolvable, must be skipped ---
    ("./local/module", ""),
    ("../shared/vpc", ""),
    ("../../modules/rds", ""),

    # --- git:: ssh scp-style (colon host:path) ---
    ("git@github.com:org/repo.git//modules/vpc?ref=v1.0", "repo/modules/vpc"),
    ("git@github.com:org/repo.git?ref=v2.1.0", "repo"),
    ("git::git@github.com:org/repo.git//modules/rds?ref=v3.0", "repo/modules/rds"),

    # --- git:: ssh:// url-style (slash host/path) ---
    ("git::ssh://git@github.com/org/repo.git//modules/vpc?ref=v1.2.3", "repo/modules/vpc"),
    ("ssh://git@github.com/org/repo.git//networking/tgw", "repo/networking/tgw"),

    # --- plain https / bare host ---
    ("github.com/org/repo//modules/vpc", "repo/modules/vpc"),
    ("https://github.com/org/repo//modules/s3", "repo/modules/s3"),
    ("https://github.com/org/repo.git//modules/s3?ref=v1", "repo/modules/s3"),

    # --- Terraform Registry shorthand: org/name/provider[//subpath] ---
    ("terraform-aws-modules/vpc/aws", "terraform-aws-vpc"),
    ("terraform-aws-modules/ecs/aws//modules/cluster", "terraform-aws-ecs/modules/cluster"),

    # --- the Registry guard: a dotted first segment is NOT a registry ref ---
    # github.com/... has a dot in segment 1 -> must fall through to host parsing,
    # NOT be mangled into "terraform-repo-..."
    ("github.com/org/repo//path", "repo/path"),

    # --- unresolvable garbage ---
    ("", ""),
    ("not-a-real-source", ""),
]


@pytest.mark.parametrize("source,expected", RESOLVE_CASES)
def test_resolve_source(source, expected):
    assert _resolve_source(source) == expected


def test_resolve_source_registry_guard_does_not_eat_host_urls():
    """Regression guard: the registry regex must not swallow host-based URLs.

    A dotted first segment (github.com) is the discriminator. If this guard
    regresses, every git source gets misparsed as a registry module and the
    whole knowledge base resolves to wrong module_refs.
    """
    assert _resolve_source("github.com/myorg/tf-modules//vpc") == "tf-modules/vpc"
    # contrast: genuine registry ref (no dot, no colon, not git-prefixed)
    assert _resolve_source("terraform-aws-modules/rds/aws") == "terraform-aws-rds"


# --------------------------------------------------------------------------
# _extract_version_ref
# --------------------------------------------------------------------------
# Each row: (source_url, body_dict, expected_ref)
# Key invariant: branch refs (main/master/develop/trunk/HEAD) must resolve to ""
# so rolling deployments never feed the `versions` convention dimension.
VERSION_CASES = [
    # --- pinned refs from source ?ref= ---
    ("git::...//mod?ref=v1.2.3", {}, "v1.2.3"),
    ("git@github.com:org/repo.git//mod?ref=1.0.0", {}, "1.0.0"),
    ("...?ref=v2.0&depth=1", {}, "v2.0"),                 # stops at &

    # --- branch refs from source: rolling, NOT a version ---
    ("git::...//mod?ref=main", {}, ""),
    ("git::...//mod?ref=master", {}, ""),
    ("git::...//mod?ref=MASTER", {}, ""),                 # case-insensitive
    ("git::...//mod?ref=develop", {}, ""),
    ("git::...//mod?ref=HEAD", {}, ""),

    # --- no ref in source -> fall back to version attribute ---
    ("terraform-aws-modules/vpc/aws", {"version": "5.1.0"}, "5.1.0"),
    ("terraform-aws-modules/vpc/aws", {"version": "~> 1.0"}, "~> 1.0"),
    ("terraform-aws-modules/vpc/aws", {"version": "main"}, ""),   # branch in version attr

    # --- source ref takes precedence over version attribute ---
    ("git::...//mod?ref=v9.9.9", {"version": "1.0.0"}, "v9.9.9"),

    # --- nothing to extract ---
    ("git::...//mod", {}, ""),
    ("", {}, ""),
]


@pytest.mark.parametrize("source,body,expected", VERSION_CASES)
def test_extract_version_ref(source, body, expected):
    assert _extract_version_ref(source, body) == expected


def test_branch_refs_excluded_from_versions_dimension():
    """A non-semver branch ref must never count as a pinned version.

    The `versions` convention asserts 'all deployments use exact semver
    pinning'. If a rolling main/master deployment leaks through as a version,
    the distilled convention becomes a lie.
    """
    for branch in ("main", "master", "develop", "trunk", "HEAD"):
        assert _extract_version_ref(f"git::...//m?ref={branch}", {}) == ""
        assert _extract_version_ref("git::...//m", {"version": branch}) == ""
