"""Unit tests for pure helper functions in app.services.retriever."""
import pytest

from app.services.retriever import (
    _semver_key,
    _build_module_source,
    _dedupe_modules,
    _build_context_text,
    _is_compose_mode,
)


# ---------------------------------------------------------------------------
# _semver_key
# ---------------------------------------------------------------------------

class TestSemverKey:
    def test_full_semver(self):
        assert _semver_key("1.2.3") == (1, 2, 3)

    def test_major_minor_only(self):
        assert _semver_key("2.5") == (2, 5, 0)

    def test_with_v_prefix(self):
        assert _semver_key("v3.1.4") == (3, 1, 4)

    def test_non_semver(self):
        assert _semver_key("latest") == (0, 0, 0)

    def test_empty_string(self):
        assert _semver_key("") == (0, 0, 0)

    def test_sorting_order(self):
        versions = ["v1.0.0", "v2.1.0", "v1.9.3", "v2.0.1", "latest"]
        result = sorted(versions, key=_semver_key)
        assert result == ["latest", "v1.0.0", "v1.9.3", "v2.0.1", "v2.1.0"]

    def test_embedded_version(self):
        """Version string with extra text around it."""
        assert _semver_key("release-1.2.3-beta") == (1, 2, 3)

    def test_large_numbers(self):
        assert _semver_key("100.200.300") == (100, 200, 300)


# ---------------------------------------------------------------------------
# _build_module_source
# ---------------------------------------------------------------------------

class TestBuildModuleSource:
    def test_ssh_url_with_tag(self):
        m = {
            "_repo_url": "git@github.com:org/infra.git",
            "module_path": "modules/vpc",
            "_latest_tag": "v1.2.0",
        }
        result = _build_module_source(m)
        assert result == "git::ssh://git@github.com/org/infra.git//modules/vpc?ref=v1.2.0"

    def test_ssh_url_without_tag(self):
        m = {
            "_repo_url": "git@github.com:org/infra.git",
            "module_path": "modules/vpc",
            "_latest_tag": "",
        }
        result = _build_module_source(m)
        assert result == "git::ssh://git@github.com/org/infra.git//modules/vpc"

    def test_https_url(self):
        m = {
            "_repo_url": "https://github.com/org/infra.git",
            "module_path": "modules/s3",
            "_latest_tag": "v3.0.0",
        }
        result = _build_module_source(m)
        assert result == "https://github.com/org/infra.git//modules/s3?ref=v3.0.0"

    def test_no_repo_url_fallback(self):
        m = {
            "_repo_url": "",
            "repo": "infra-modules",
            "module_path": "modules/rds",
            "_latest_tag": "v1.0.0",
        }
        result = _build_module_source(m)
        assert result == "infra-modules//modules/rds"

    def test_missing_keys_graceful(self):
        """All keys missing - should not raise."""
        result = _build_module_source({})
        assert result == "//"


# ---------------------------------------------------------------------------
# _dedupe_modules
# ---------------------------------------------------------------------------

class TestDedupeModules:
    def test_keeps_highest_similarity(self):
        rows = [
            {"repo": "r", "module_path": "m/a", "similarity": 0.7},
            {"repo": "r", "module_path": "m/a", "similarity": 0.9},
        ]
        result = _dedupe_modules(rows)
        assert len(result) == 1
        assert result[0]["similarity"] == 0.9

    def test_different_modules_kept(self):
        rows = [
            {"repo": "r", "module_path": "m/a", "similarity": 0.8},
            {"repo": "r", "module_path": "m/b", "similarity": 0.6},
        ]
        result = _dedupe_modules(rows)
        assert len(result) == 2

    def test_sorted_by_similarity_desc(self):
        rows = [
            {"repo": "r", "module_path": "m/a", "similarity": 0.5},
            {"repo": "r", "module_path": "m/b", "similarity": 0.9},
            {"repo": "r", "module_path": "m/c", "similarity": 0.7},
        ]
        result = _dedupe_modules(rows)
        sims = [r["similarity"] for r in result]
        assert sims == [0.9, 0.7, 0.5]

    def test_empty_input(self):
        assert _dedupe_modules([]) == []

    def test_missing_similarity(self):
        """Rows without similarity key should default to 0."""
        rows = [
            {"repo": "r", "module_path": "m/a"},
            {"repo": "r", "module_path": "m/a", "similarity": 0.5},
        ]
        result = _dedupe_modules(rows)
        assert len(result) == 1
        assert result[0]["similarity"] == 0.5


# ---------------------------------------------------------------------------
# _build_context_text
# ---------------------------------------------------------------------------

class TestBuildContextText:
    def _make_module(self, **overrides):
        base = {
            "module_name": "vpc",
            "repo": "infra",
            "tags": ["networking"],
            "description": "VPC module",
            "_repo_url": "",
            "_latest_tag": "",
            "module_path": "modules/vpc",
            "variables": {},
            "outputs": {},
            "resources": [],
        }
        base.update(overrides)
        return base

    def test_basic_output(self):
        m = self._make_module()
        text = _build_context_text([m])
        assert "Module: vpc (repo: infra)" in text
        assert "Tags: networking" in text
        assert "Description: VPC module" in text

    def test_variables_formatted(self):
        m = self._make_module(
            variables={
                "cidr": {
                    "type": "string",
                    "required": True,
                    "description": "CIDR block",
                    "default": None,
                },
                "name": {
                    "type": "string",
                    "required": False,
                    "description": "Name",
                    "default": "main",
                },
            }
        )
        text = _build_context_text([m])
        assert "Variables:" in text
        assert "cidr: type=string, required" in text
        assert "name: type=string, optional" in text
        assert 'default=main' in text

    def test_outputs_formatted(self):
        m = self._make_module(
            outputs={
                "vpc_id": {"description": "The VPC ID"},
                "plain": {"description": ""},
            }
        )
        text = _build_context_text([m])
        assert "Outputs:" in text
        assert "vpc_id: The VPC ID" in text
        assert "  - plain" in text

    def test_resources_formatted(self):
        m = self._make_module(resources=["aws_vpc", "aws_subnet"])
        text = _build_context_text([m])
        assert "Resources: aws_vpc, aws_subnet" in text

    def test_multiple_modules_separator(self):
        m1 = self._make_module(module_name="vpc")
        m2 = self._make_module(module_name="rds")
        text = _build_context_text([m1, m2])
        assert "\n\n---\n\n" in text

    def test_empty_tags(self):
        m = self._make_module(tags=[])
        text = _build_context_text([m])
        assert "Tags: " in text

    def test_empty_list(self):
        assert _build_context_text([]) == ""


# ---------------------------------------------------------------------------
# _is_compose_mode
# ---------------------------------------------------------------------------

class TestIsComposeMode:
    def test_compose(self):
        assert _is_compose_mode("compose") is True

    def test_generate_alias(self):
        assert _is_compose_mode("generate") is True

    def test_optimize(self):
        assert _is_compose_mode("optimize") is False

    def test_audit(self):
        assert _is_compose_mode("audit") is False

    def test_search(self):
        assert _is_compose_mode("search") is False

    def test_empty_string(self):
        assert _is_compose_mode("") is False
