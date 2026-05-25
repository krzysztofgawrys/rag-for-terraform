"""Backfill module_dependencies from raw_code stored in PostgreSQL.

Extracts module{} source values from raw_code using regex (no HCL parser —
faster and more robust for large codebases) and populates the
module_dependencies table.

Usage:
    docker compose exec api python -m scripts.backfill_dependencies
"""

import asyncio
import re
import sys
from pathlib import PurePosixPath

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, str(PurePosixPath(__file__).parent.parent))
from app.core.graph import _parse_dep_source  # noqa: E402

log = structlog.get_logger()

def _extract_dep_sources(raw_code: str, module_path: str) -> list[str]:
    """Extract module{} source values from raw HCL code.

    Strategy: scan line-by-line. When we see `module "..." {`, set a flag.
    When inside a module block and we see `source = "..."`, capture it.
    Track brace depth to know when the block ends.
    """
    sources = []
    in_module = False
    brace_depth = 0

    for line in raw_code.splitlines():
        stripped = line.strip()

        if not in_module:
            if re.match(r'module\s+"[^"]+"\s*\{', stripped):
                in_module = True
                brace_depth = 1
                # source might be on same line (unlikely but handle it)
                m = re.search(r'source\s*=\s*"([^"]+)"', stripped)
                if m:
                    sources.append(m.group(1))
        else:
            brace_depth += stripped.count("{") - stripped.count("}")
            m = re.search(r'source\s*=\s*"([^"]+)"', stripped)
            if m and brace_depth >= 1:
                sources.append(m.group(1))
            if brace_depth <= 0:
                in_module = False

    result = []
    for source in sources:
        if source.startswith("./") or source.startswith("../"):
            module_dir = PurePosixPath(module_path)
            joined = module_dir / source
            parts = []
            for p in joined.parts:
                if p == "..":
                    if parts:
                        parts.pop()
                elif p != ".":
                    parts.append(p)
            source = "/".join(parts) if parts else source
        result.append(source)
    return result


async def backfill(database_url: str) -> None:
    engine = create_async_engine(database_url, echo=False, pool_size=5)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(text(
            "SELECT repo, module_path, version, raw_code FROM modules "
            "WHERE raw_code IS NOT NULL AND raw_code != ''"
        ))
        rows = result.fetchall()
        total = len(rows)
        log.info("backfill_start", modules=total)

        inserted = 0
        skipped = 0
        modules_with_deps = 0

        for i, row in enumerate(rows):
            repo = row.repo
            module_path = row.module_path
            version = row.version

            dep_sources = _extract_dep_sources(row.raw_code, module_path)
            if not dep_sources:
                continue

            modules_with_deps += 1

            await db.execute(
                text("""
                    DELETE FROM module_dependencies
                    WHERE parent_repo = :repo
                      AND parent_path = :path
                      AND parent_version = :version
                """),
                dict(repo=repo, path=module_path, version=version),
            )

            for dep_source in dep_sources:
                dep_repo, dep_path, dep_version = _parse_dep_source(
                    dep_source, fallback_repo=repo, fallback_version=version,
                )
                try:
                    await db.execute(
                        text("""
                            INSERT INTO module_dependencies
                                (parent_repo, parent_path, parent_version,
                                 dep_repo, dep_path, dep_version, dep_name)
                            VALUES (:pr, :pp, :pv, :dr, :dp, :dv, :dn)
                            ON CONFLICT DO NOTHING
                        """),
                        dict(
                            pr=repo, pp=module_path, pv=version,
                            dr=dep_repo, dp=dep_path, dv=dep_version,
                            dn=dep_path.strip("/").split("/")[-1] if dep_path else dep_repo,
                        ),
                    )
                    inserted += 1
                except Exception as e:
                    log.warning("insert_failed", dep=dep_source, error=str(e))
                    skipped += 1

            # Commit every 500 modules
            if (i + 1) % 500 == 0:
                await db.commit()
                log.info("progress", processed=i + 1, total=total,
                         inserted=inserted, with_deps=modules_with_deps)

        await db.commit()

    log.info("backfill_done", inserted=inserted, skipped=skipped,
             modules_with_deps=modules_with_deps, total=total)
    await engine.dispose()


def main():
    from app.core.config import get_settings
    settings = get_settings()
    asyncio.run(backfill(settings.database_url))


if __name__ == "__main__":
    main()
