"""
Git Fetcher — on-demand code fragment retrieval from git repos.

Fetches HCL code fragments by source_locator, caches in Redis (5 min TTL).
NEVER persists to PostgreSQL — code lives in git only.

source_locator format: 'consumer-repo@abc1234:path/to/file.tf:L42-L78'
  - repo@commit:path (required)
  - :Lstart-Lend (optional line range)
"""

import re
import os
import structlog
from app.core.config import get_settings

log = structlog.get_logger()
settings = get_settings()

_CACHE_TTL = 300  # 5 minutes


async def fetch_fragment(source_locator: str) -> str | None:
    """Fetch a code fragment from git via source_locator.

    Uses Redis cache (5 min TTL). Falls back to git show if not cached.
    Returns None if fragment cannot be fetched.
    """
    import hashlib
    import redis.asyncio as aioredis

    cache_key = f"git_frag:{hashlib.md5(source_locator.encode()).hexdigest()}"

    try:
        r = aioredis.from_url(settings.redis_url)
        cached = await r.get(cache_key)
        if cached:
            await r.aclose()
            return cached.decode()
    except Exception:
        r = None

    # Parse locator
    parsed = _parse_locator(source_locator)
    if not parsed:
        return None

    repo_name, commit, file_path, line_start, line_end = parsed

    # Try to fetch from local repo cache
    fragment = await _fetch_from_cache(repo_name, commit, file_path, line_start, line_end)

    if fragment and r:
        try:
            await r.setex(cache_key, _CACHE_TTL, fragment)
            await r.aclose()
        except Exception:
            pass

    return fragment


def _parse_locator(source_locator: str) -> tuple | None:
    """Parse 'repo@commit:path/file.tf:L42-L78' into components."""
    # repo@commit:path:Lstart-Lend
    m = re.match(r'^([^@]+)(?:@([^:]+))?:(.+?)(?::L(\d+)(?:-L?(\d+))?)?$', source_locator)
    if not m:
        log.warning("invalid_source_locator", locator=source_locator)
        return None

    repo_name = m.group(1)
    commit = m.group(2) or "HEAD"
    file_path = m.group(3)
    line_start = int(m.group(4)) if m.group(4) else None
    line_end = int(m.group(5)) if m.group(5) else None

    return repo_name, commit, file_path, line_start, line_end


async def _fetch_from_cache(
    repo_name: str,
    commit: str,
    file_path: str,
    line_start: int | None,
    line_end: int | None,
) -> str | None:
    """Fetch file content from local repo cache directory."""
    import asyncio

    repo_dir = os.path.join(settings.repo_cache_dir, repo_name)
    if not os.path.isdir(repo_dir):
        log.warning("repo_not_in_cache", repo=repo_name)
        return None

    try:
        # Use git show to get file at specific commit
        # -c safe.directory: worker may clone as different UID than API
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", f"safe.directory={repo_dir}", "show", f"{commit}:{file_path}",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            log.warning("git_show_failed", repo=repo_name, commit=commit,
                        file=file_path, error=stderr.decode()[:200])
            return None

        content = stdout.decode()

        # Apply line range if specified
        if line_start is not None:
            lines = content.splitlines()
            start = max(0, line_start - 1)  # 1-indexed → 0-indexed
            end = line_end if line_end else start + 1
            content = "\n".join(lines[start:end])

        return content

    except Exception as e:
        log.warning("git_fetch_error", repo=repo_name, error=str(e))
        return None
