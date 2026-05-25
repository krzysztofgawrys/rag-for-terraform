"""Prompt loader for external .md prompt files.

All prompts live as plain .md files under app/prompts/.  This module provides
helpers to load them at import time (cached) so the rest of the codebase
works with plain strings, same as before.
"""

from pathlib import Path
from functools import lru_cache

_PROMPTS_DIR = Path(__file__).parent


@lru_cache(maxsize=128)
def load_prompt(relative_path: str) -> str:
    """Load a prompt file and return its content as a stripped string.

    Args:
        relative_path: path relative to app/prompts/, e.g. "agent/compose.md"
    """
    path = _PROMPTS_DIR / relative_path
    return path.read_text(encoding="utf-8").strip()


def load_prompt_sections(relative_path: str) -> dict[str, str]:
    """Load a prompt file that uses ``<!-- key -->`` comment delimiters.

    Returns a dict mapping each key to the text that follows it (stripped).
    Useful for files like embeddings.md or fewshot files with input/output.
    """
    raw = load_prompt(relative_path)
    sections: dict[str, str] = {}
    current_key: str | None = None
    buf: list[str] = []

    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!-- ") and stripped.endswith(" -->"):
            if current_key is not None:
                sections[current_key] = "\n".join(buf).strip()
            current_key = stripped[5:-4].strip()
            buf = []
        else:
            buf.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(buf).strip()

    return sections
