"""
migration.py — JSONL schema migration logic.

Extracted from db.py to isolate schema versioning and migration concerns,
making them independently testable and keeping the Database facade thin.

Functions here are synchronous (called via asyncio.to_thread) and operate
on raw Path / str values with no Database class dependency.
"""

from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from src.core.errors import NonCriticalCategory, log_noncritical
from src.db.db_utils import _JSONL_MIGRATIONS, _JSONL_SCHEMA_VERSION, _build_jsonl_header
from src.utils import json_dumps, json_loads

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "batch_ensure_jsonl_schema",
    "ensure_jsonl_schema",
    "apply_jsonl_migrations",
]


def ensure_jsonl_schema(
    file_path: Path,
    invalidate_fn: Any = None,
) -> None:
    """Ensure a JSONL file has the current schema header.

    Args:
        file_path: Path to the .jsonl file.
        invalidate_fn: Callable to invalidate cached file handles for *file_path*
            (e.g. ``FileHandlePool.invalidate``).  Optional — pass ``None`` when
            running outside the Database context.
    """
    if not file_path.exists() or file_path.stat().st_size == 0:
        return

    with file_path.open("r", encoding="utf-8") as f:
        first_line = f.readline().strip()

    if not first_line:
        return

    try:
        parsed = json_loads(first_line)
    except Exception:
        log_noncritical(
            NonCriticalCategory.FILE_PARSING,
            "Failed to parse JSONL header in %s",
            logger=log,
            extra={"file": str(file_path)},
        )
        return

    if isinstance(parsed, dict) and parsed.get("type") == "header":
        version = parsed.get("_version", 0)
        if version < _JSONL_SCHEMA_VERSION:
            apply_jsonl_migrations(file_path, version, invalidate_fn)
        return

    # No header — legacy file, prepend header
    content = file_path.read_text(encoding="utf-8")
    if invalidate_fn is not None:
        invalidate_fn(file_path)
    header = _build_jsonl_header()
    file_path.write_text(header + content, encoding="utf-8")
    log.info(
        "Added JSONL schema v%d header to %s",
        _JSONL_SCHEMA_VERSION,
        file_path.name,
    )


def batch_ensure_jsonl_schema(
    file_paths: list[Path],
    invalidate_fn: Any = None,
) -> list[tuple[str, str]]:
    """Run ensure_jsonl_schema on multiple files in a single thread hop.

    Returns a list of ``(filename, error_message)`` tuples for any files
    that failed migration, so callers can log errors without raising.
    """
    errors: list[tuple[str, str]] = []
    for fp in file_paths:
        try:
            ensure_jsonl_schema(fp, invalidate_fn)
        except Exception as exc:  # noqa: BLE001
            errors.append((fp.name, str(exc)))
    return errors


def apply_jsonl_migrations(
    file_path: Path,
    current_version: int,
    invalidate_fn: Any = None,
) -> None:
    """Apply incremental JSONL schema migrations.

    Args:
        file_path: Path to the .jsonl file.
        current_version: Schema version found in the file's header.
        invalidate_fn: Callable to invalidate cached file handles for *file_path*.
    """
    if not _JSONL_MIGRATIONS:
        return

    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    migrated: list[str] = []
    header_written = False

    for line in lines:
        if not line.strip():
            continue
        try:
            msg = json_loads(line)
        except Exception:
            log_noncritical(
                NonCriticalCategory.DB_OPERATION,
                "Skipping unparseable line during JSONL migration in %s",
                logger=log,
                extra={"file": str(file_path)},
            )
            migrated.append(line)
            continue

        if msg.get("type") == "header" and not header_written:
            new_header = json_dumps({"_version": _JSONL_SCHEMA_VERSION, "type": "header"})
            migrated.append(new_header)
            header_written = True
            continue

        for target_ver, fns in _JSONL_MIGRATIONS:
            if current_version < target_ver:
                for fn in fns:
                    msg = fn(msg)

        migrated.append(json_dumps(msg, ensure_ascii=False))

    if not header_written:
        header = json_dumps({"_version": _JSONL_SCHEMA_VERSION, "type": "header"})
        migrated.insert(0, header)

    if invalidate_fn is not None:
        invalidate_fn(file_path)
    file_path.write_text("\n".join(migrated) + "\n", encoding="utf-8")
    log.info(
        "Migrated JSONL schema v%d→v%d for %s",
        current_version,
        _JSONL_SCHEMA_VERSION,
        file_path.name,
    )
