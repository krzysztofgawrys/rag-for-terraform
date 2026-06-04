"""Unit tests for convention_distiller constants and prompt building.

extract_assessment / strip_preamble tests live in test_distiller_parsing.py
(parametrized, with latent-bug documentation).
"""
import pytest

from app.services.convention_distiller import (
    build_distillation_prompt,
    DIMENSIONS,
)


# ---------------------------------------------------------------------------
# DIMENSIONS constant
# ---------------------------------------------------------------------------

class TestDimensions:
    def test_all_six_present(self):
        assert len(DIMENSIONS) == 6

    def test_expected_dimensions(self):
        expected = {"naming", "vars", "codeploy", "tagging", "layout", "versions"}
        assert set(DIMENSIONS) == expected


# ---------------------------------------------------------------------------
# build_distillation_prompt
# ---------------------------------------------------------------------------

class TestBuildDistillationPrompt:
    def test_contains_dimension(self):
        prompt = build_distillation_prompt("org/vpc", "naming", ["usage1"])
        assert "**naming**" in prompt
        assert "org/vpc" in prompt

    def test_contains_all_usages(self):
        usages = ["deploy A as foo-prod", "deploy B as bar-dev", "deploy C as baz-stg"]
        prompt = build_distillation_prompt("org/vpc", "naming", usages)
        for u in usages:
            assert f"- {u}" in prompt

    def test_stats_line(self):
        usages = ["u1", "u2", "u3"]
        prompt = build_distillation_prompt("org/vpc", "vars", usages)
        assert "(3 usages total)" in prompt

    def test_single_usage(self):
        prompt = build_distillation_prompt("org/rds", "versions", ["single usage"])
        assert "(1 usages total)" in prompt

    def test_ends_with_instruction(self):
        prompt = build_distillation_prompt("org/vpc", "tagging", ["u1"])
        assert "ASSESSMENT line" in prompt

    def test_all_dimensions_accepted(self):
        """build_distillation_prompt should work for all 6 dimensions."""
        for dim in DIMENSIONS:
            prompt = build_distillation_prompt("org/mod", dim, ["usage"])
            assert dim in prompt
