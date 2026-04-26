"""
src/db/db_utils.py — Shared constants, pure utility functions for the DB layer.

Extracted from db.py to avoid circular imports between decomposed modules.
Every function here is a pure utility with no Database class dependency.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from src.utils import json_dumps
from src.utils.path import sanitize_path_component as _sanitize_chat_id_for_path
from src.core.errors import NonCriticalCategory, log_noncritical

log = logging.getLogger(__name__)

# ── constants ───────────────────────────────────────────────────────────────

# Pattern for valid chat_id (safe for file paths)
_CHAT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-.@]+$")

# Maximum length for the 'name' field persisted to JSONL
_MAX_NAME_LENGTH = 200

# Control characters stripped from names
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

# Maximum messages that can be retrieved in a single query (memory safety)
MAX_MESSAGE_HISTORY = 500

# Maximum entries in the message ID index
MAX_MESSAGE_ID_INDEX = 100_000

# JSONL schema version
_JSONL_SCHEMA_VERSION = 1
_JSONL_MIGRATIONS: list[tuple[int, list[Any]]] = []


# ── validation ─────────────────────────────────────────────────────────────


def _validate_chat_id(chat_id: str) -> None:
    """Validate chat_id format for safe file path usage.

    Raises:
        ValueError: If chat_id contains unsafe characters.
    """
    if not chat_id:
        raise ValueError("chat_id cannot be empty")
    if not _CHAT_ID_PATTERN.match(chat_id):
        raise ValueError(
            f"Invalid chat_id format: {chat_id!r}. "
            "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
        )


def _sanitize_name(name: Optional[str]) -> Optional[str]:
    """Sanitize a sender/tool name before persisting to JSONL.

    Strips control characters and truncates to ``_MAX_NAME_LENGTH``.
    Returns ``None`` if the name is empty after sanitization.
    """
    if not name:
        return None
    cleaned = _CONTROL_CHAR_PATTERN.sub("", name)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_NAME_LENGTH:
        cleaned = cleaned[:_MAX_NAME_LENGTH]
    return cleaned


# ── JSONL helpers ──────────────────────────────────────────────────────────


def _build_jsonl_header() -> str:
    """Return the JSONL schema version header line (with trailing newline)."""
    return json_dumps({"_version": _JSONL_SCHEMA_VERSION, "type": "header"}) + "\n"


# ── metrics helpers ────────────────────────────────────────────────────────


def _track_db_latency(elapsed_seconds: float) -> None:
    """Record a database operation latency in the global metrics collector."""
    try:
        from src.monitoring.performance import get_metrics_collector

        get_metrics_collector().track_db_latency(elapsed_seconds)
    except Exception:
        log_noncritical(
            NonCriticalCategory.DB_TRACKING,
            "Failed to track DB latency (%.3fs)",
            elapsed_seconds,
            logger=log,
        )


def _track_db_write_latency(elapsed_seconds: float) -> None:
    """Record a database *write* operation latency in the global metrics collector."""
    try:
        from src.monitoring.performance import get_metrics_collector

        get_metrics_collector().track_db_write_latency(elapsed_seconds)
    except Exception:
        log_noncritical(
            NonCriticalCategory.DB_TRACKING,
            "Failed to track DB write latency (%.3fs)",
            elapsed_seconds,
            logger=log,
        )


# ── logging helpers ────────────────────────────────────────────────────────


def _db_log_extra(chat_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Build structured extra dict for DB log statements with correlation ID."""
    from src.logging import get_correlation_id

    extra: dict[str, Any] = {"correlation_id": get_correlation_id()}
    if chat_id is not None:
        extra["chat_id"] = chat_id
    extra.update(kwargs)
    return extra


# ── disk space / atomic write ──────────────────────────────────────────────


def _check_disk_space_before_write(path: Path) -> None:
    """Check disk space before write operations. Raises DiskSpaceError if low."""
    from src.utils import DEFAULT_MIN_DISK_SPACE, check_disk_space
    from src.exceptions import DiskSpaceError

    try:
        result = check_disk_space(path, min_bytes=DEFAULT_MIN_DISK_SPACE)
        if not result.has_sufficient_space:
            raise DiskSpaceError(
                f"Insufficient disk space for write operation",
                path=str(path),
                free_mb=round(result.free_mb, 2),
                required_mb=round(DEFAULT_MIN_DISK_SPACE / (1024 * 1024), 2),
            )
    except OSError as e:
        log.warning("Could not verify disk space for %s: %s", path, e)


def _atomic_write(file_path: Path, content: str) -> None:
    """Synchronous helper for atomic file writes with disk-space check."""
    from src.utils.async_file import sync_atomic_write

    _check_disk_space_before_write(file_path)
    sync_atomic_write(file_path, content)
