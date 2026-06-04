"""Unit tests for app.services.convention_distiller text parsing.

Covers the two pure regex functions that post-process raw LLM output before
it is stored as an "authoritative" convention. Silent breakage here corrupts
the knowledge base without raising anything:

  - extract_assessment: split convention text from its STRONG/MODERATE/...
                        confidence label (-> ("...text...", "LABEL"))
  - strip_preamble:     remove LLM chatter ("Here is the convention:", bold
                        headers, markdown headers) without eating real content

Run:  pytest tests/test_distiller_parsing.py -v
"""

import pytest

from app.services.convention_distiller import extract_assessment, strip_preamble


# --------------------------------------------------------------------------
# extract_assessment
# --------------------------------------------------------------------------
# Each row: (raw_text, expected_content, expected_assessment)
ASSESSMENT_CASES = [
    # --- standard: label on its own trailing line ---
    (
        "Buckets pin v2.3.0 in 12/14 deployments.\nASSESSMENT: STRONG",
        "Buckets pin v2.3.0 in 12/14 deployments.",
        "STRONG",
    ),
    # --- inline, no newline before label ---
    ("Tagging is consistent. ASSESSMENT: MODERATE", "Tagging is consistent.", "MODERATE"),
    # --- case-insensitive match, label normalised to upper ---
    ("Naming varies widely. assessment: weak", "Naming varies widely.", "WEAK"),
    # --- multi-token label kinds survive (underscore is non-space) ---
    ("Only one usage seen. ASSESSMENT: LOW_EVIDENCE", "Only one usage seen.", "LOW_EVIDENCE"),
    # --- no space after colon ---
    ("Layout is flat. ASSESSMENT:STRONG", "Layout is flat.", "STRONG"),
    # --- duplicated ASSESSMENT block: FIRST wins, remainder discarded ---
    (
        "Vars use snake_case.\nASSESSMENT: STRONG\nrestated\nASSESSMENT: WEAK",
        "Vars use snake_case.",
        "STRONG",
    ),
    # --- no assessment line at all -> UNKNOWN, content preserved ---
    ("Convention text with no label.", "Convention text with no label.", "UNKNOWN"),
    ("", "", "UNKNOWN"),
]


@pytest.mark.parametrize("text,exp_content,exp_assessment", ASSESSMENT_CASES)
def test_extract_assessment(text, exp_content, exp_assessment):
    content, assessment = extract_assessment(text)
    assert content == exp_content
    assert assessment == exp_assessment


def test_extract_assessment_only_first_token_captured():
    """\\w+ captures the first word-character run after the colon.

    Trailing parentheticals are dropped (good), so a label written as
    'STRONG (>80% consistency)' resolves cleanly to 'STRONG'.
    """
    _, a = extract_assessment("text\nASSESSMENT: STRONG (>80% consistency)")
    assert a == "STRONG"


def test_extract_assessment_trailing_punctuation_is_stripped():
    """Regression guard for the trailing-punctuation fix (\\S+ -> \\w+).

    \\w+ stops at the first non-word character, so a trailing period is left
    OUT of the label: 'ASSESSMENT: STRONG.' yields 'STRONG', which matches a
    downstream equality/enum check against {STRONG, MODERATE, WEAK, ...}.

    Previously \\S+ captured 'STRONG.' (period included), which silently
    dropped the convention out of stale-filtering / RAG injection.
    """
    _, a = extract_assessment("text\nASSESSMENT: STRONG.")
    assert a == "STRONG"


# --------------------------------------------------------------------------
# strip_preamble
# --------------------------------------------------------------------------
# Each row: (raw_text, expected_after_strip)
PREAMBLE_CASES = [
    # --- the chatter we WANT removed ---
    ("Here is the convention: Buckets are encrypted.", "Buckets are encrypted."),
    ("Here's my analysis:\nModules pin exact tags.", "Modules pin exact tags."),
    ("**Naming**\nResources use kebab-case.", "Resources use kebab-case."),
    ("## Tagging\nAll resources carry Owner.", "All resources carry Owner."),

    # --- content we MUST keep untouched (false-positive guards) ---
    # plain declarative first sentence, no preamble markers
    ("Resources use the s3 module across 12 repos.", "Resources use the s3 module across 12 repos."),
    # starts with capital + a noun that is NOT convention/analysis/paragraph
    ("Tagging uses Environment and Owner keys in 18/20 deployments.",
     "Tagging uses Environment and Owner keys in 18/20 deployments."),
    # "The" + unrelated noun must survive (pattern3 needs convention/analysis/paragraph)
    ("The bucket is encrypted with KMS in every deployment.",
     "The bucket is encrypted with KMS in every deployment."),
    # empty
    ("", ""),
]


@pytest.mark.parametrize("text,expected", PREAMBLE_CASES)
def test_strip_preamble(text, expected):
    assert strip_preamble(text) == expected


def test_strip_preamble_does_not_eat_real_content():
    """The single most important guard: stripping must be conservative.

    If strip_preamble ever swallows the first real sentence of a convention,
    the stored 'authoritative' guidance is silently truncated. None of these
    realistic convention openers should lose a single character.
    """
    keep = [
        "Most deployments set force_destroy = false (19/21).",
        "Environment tags are mandatory; 20/20 deployments include them.",
        "Version pinning is strict: all 14 usages reference exact semver tags.",
    ]
    for text in keep:
        assert strip_preamble(text) == text
