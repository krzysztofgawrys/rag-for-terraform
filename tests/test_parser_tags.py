"""Edge-case tests for parser._extract_tags (pure function, files on disk).

_extract_tags fuses four tag sources into one sorted, de-duplicated list:
  1. explicit files: tags.txt / .tags / TAGS  (one tag per line)
  2. locals.tf:      locals { tags = {...} | [...] }
  3. path segments:  folders, minus a skip-set, minus the module dir name
  4. resource types: aws_s3_bucket -> s3, google_compute_instance -> compute

Silent breakage here mis-tags modules and corrupts tag-filtered retrieval, so
each source (and their interaction) gets its own guard. No DB, no mocks - real
temp dirs via tmp_path.
"""
from pathlib import Path

import pytest

from app.core.parser import _extract_tags


# ---------------------------------------------------------------------------
# 1. Explicit tag files: tags.txt / .tags / TAGS
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("filename", ["tags.txt", ".tags", "TAGS"])
def test_explicit_tag_file_one_per_line(tmp_path: Path, filename):
    (tmp_path / filename).write_text("vpc\nnetworking\n\n   prod   \n")
    # module_path with no extra folders, no resources -> only the file's tags
    assert _extract_tags(tmp_path, "vpc", []) == ["networking", "prod", "vpc"]


def test_explicit_tag_files_are_unioned(tmp_path: Path):
    (tmp_path / "tags.txt").write_text("alpha\n")
    (tmp_path / ".tags").write_text("beta\n")
    (tmp_path / "TAGS").write_text("alpha\ngamma\n")  # alpha duplicates -> deduped
    assert _extract_tags(tmp_path, "mod", []) == ["alpha", "beta", "gamma"]


def test_blank_and_whitespace_lines_skipped(tmp_path: Path):
    (tmp_path / "tags.txt").write_text("\n\n  \n\tone\t\n")
    assert _extract_tags(tmp_path, "mod", []) == ["one"]


# ---------------------------------------------------------------------------
# 2. locals.tf - tags as a DICT (values taken) vs a LIST (items taken)
# ---------------------------------------------------------------------------
def test_locals_tf_tags_as_dict_uses_values(tmp_path: Path):
    (tmp_path / "locals.tf").write_text(
        'locals {\n  tags = {\n    Environment = "prod"\n    Team = "platform"\n  }\n}\n'
    )
    assert _extract_tags(tmp_path, "mod", []) == ["platform", "prod"]


def test_locals_tf_tags_as_list_uses_items(tmp_path: Path):
    (tmp_path / "locals.tf").write_text('locals {\n  tags = ["alpha", "beta"]\n}\n')
    assert _extract_tags(tmp_path, "mod", []) == ["alpha", "beta"]


def test_locals_tf_malformed_is_swallowed(tmp_path: Path):
    # Invalid HCL must not crash extraction; other sources still contribute.
    (tmp_path / "locals.tf").write_text("locals { tags = [ this is not valid hcl ")
    (tmp_path / "tags.txt").write_text("survives\n")
    assert _extract_tags(tmp_path, "mod", []) == ["survives"]


def test_locals_tf_without_tags_key_contributes_nothing(tmp_path: Path):
    (tmp_path / "locals.tf").write_text('locals {\n  name = "x"\n}\n')
    assert _extract_tags(tmp_path, "mod", []) == []


# ---------------------------------------------------------------------------
# 3. Path segments - folders become tags, skip-set and module dir excluded
# ---------------------------------------------------------------------------
def test_path_segments_become_tags_skipping_module_dir(tmp_path: Path):
    # "networking/vpc" -> folder "networking" tagged, "vpc" (module dir) is not
    assert _extract_tags(tmp_path, "networking/vpc", []) == ["networking"]


def test_path_skip_folders_dropped(tmp_path: Path):
    # "modules" / "module" / "terraform" / "templates" / "products" are skipped
    assert _extract_tags(tmp_path, "modules/networking/vpc", []) == ["networking"]
    assert _extract_tags(tmp_path, "terraform/templates/foo", []) == []


def test_path_segments_lowercased(tmp_path: Path):
    assert _extract_tags(tmp_path, "Networking/VPC", []) == ["networking"]


# ---------------------------------------------------------------------------
# 4. Resource-type tags - cloud_service_kind -> service
# ---------------------------------------------------------------------------
def test_resource_types_yield_service_tags(tmp_path: Path):
    resources = [
        "aws_s3_bucket",
        "aws_lambda_function",
        "google_compute_instance",
        "azurerm_storage_account",
    ]
    assert _extract_tags(tmp_path, "mod", resources) == [
        "compute", "lambda", "s3", "storage",
    ]


def test_resource_non_cloud_prefix_ignored(tmp_path: Path):
    # prefix not in {aws, azurerm, google} -> not tagged
    assert _extract_tags(tmp_path, "mod", ["random_pet", "null_resource"]) == []


def test_resource_too_short_ignored(tmp_path: Path):
    # need >= 3 underscore-parts; "aws_vpc" has only 2 -> skipped
    assert _extract_tags(tmp_path, "mod", ["aws_vpc"]) == []


def test_resource_single_char_service_ignored(tmp_path: Path):
    # service segment must be longer than 1 char
    assert _extract_tags(tmp_path, "mod", ["aws_s_bucket"]) == []


# ---------------------------------------------------------------------------
# Interaction: all four sources fused, de-duplicated, sorted
# ---------------------------------------------------------------------------
def test_all_sources_fused_dedup_sorted(tmp_path: Path):
    (tmp_path / "tags.txt").write_text("networking\ncustom\n")
    (tmp_path / "locals.tf").write_text('locals {\n  tags = ["prod"]\n}\n')
    # path adds "networking" (dup), resources add "s3"
    out = _extract_tags(tmp_path, "networking/vpc", ["aws_s3_bucket"])
    assert out == ["custom", "networking", "prod", "s3"]


def test_no_sources_returns_empty(tmp_path: Path):
    assert _extract_tags(tmp_path, "vpc", []) == []
