"""
src/db/db_index.py — Message ID index management and recovery.

Standalone functions for loading, rebuilding, and recovering the
message-ID index used for O(1) duplicate detection. Called by
Database thin wrappers to keep db.py focused on core CRUD.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Set

from src.utils import JSONDecodeError, json_dumps, json_loads

log = logging.getLogger(__name__)


# ── dataclass ───────────────────────────────────────────────────────────────


@dataclass(slots=True)
class RecoveryResult:
    """Result of message index recovery operation."""

    recovered: bool
    preserved_count: int = 0
    rebuilt_count: int = 0
    total_count: int = 0
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)


# ── ID validation helpers ───────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
_UUID_SEARCH_RE = re.compile(
    r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
    re.IGNORECASE,
)


def is_valid_id(id_str: str) -> bool:
    """Check if a string looks like a valid message ID."""
    if _UUID_RE.match(id_str):
        return True
    # Accept alphanumeric IDs with dashes/underscores (min 8 chars)
    if len(id_str) >= 8 and all(c.isalnum() or c in "-_" for c in id_str):
        return True
    return False


def extract_valid_ids(content: str) -> Set[str]:
    """
    Extract valid message IDs from potentially corrupted JSON content.

    Uses two strategies:
    1. Line-by-line parsing (for partially written files).
    2. UUID pattern extraction.
    """
    valid_ids: Set[str] = set()

    # Strategy 1: line-by-line
    for line in content.splitlines():
        line = line.strip().strip(',[]"')
        if line and is_valid_id(line):
            valid_ids.add(line)

    # Strategy 2: UUID regex
    for match in _UUID_SEARCH_RE.finditer(content):
        valid_ids.add(match.group())

    return valid_ids


# ── scanning ────────────────────────────────────────────────────────────────


def scan_message_files(messages_dir: Path) -> Set[str]:
    """
    Scan all .jsonl message files and extract message IDs.

    Returns:
        Set of all message IDs found.
    """
    ids: Set[str] = set()

    if not messages_dir.exists():
        return ids

    for msg_file in messages_dir.glob("*.jsonl"):
        try:
            content = msg_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                if line.strip():
                    try:
                        msg = json_loads(line)
                        msg_id = msg.get("id")
                        if msg_id:
                            ids.add(msg_id)
                    except JSONDecodeError:
                        continue
        except OSError as exc:
            log.warning("Failed to read message file %s: %s", msg_file.name, exc)
            continue

    return ids


# ── index lifecycle ─────────────────────────────────────────────────────────


def load_index(index_file: Path) -> Optional[Set[str]]:
    """
    Try to load the persisted message index.

    Returns:
        Set of IDs on success, None if file is missing or corrupt.
    """
    if not index_file.exists():
        return None

    try:
        content = index_file.read_text(encoding="utf-8")
        data = json_loads(content)
        if isinstance(data, list):
            log.debug("Loaded message index with %d entries", len(data))
            return set(data)
        log.warning("message_index.json has invalid format (expected list)")
        return None
    except JSONDecodeError as exc:
        log.warning("message_index.json is corrupted: %s", exc)
        return None
    except OSError as exc:
        log.warning("Failed to read message_index.json: %s", exc)
        return None


def save_index(index_file: Path, ids: Set[str], atomic_write_fn) -> None:
    """Persist message ID index to disk via atomic write."""
    content = json_dumps(list(ids), ensure_ascii=False)
    atomic_write_fn(index_file, content)


def rebuild_index(messages_dir: Path) -> Set[str]:
    """
    Rebuild the message ID index by scanning all message files.

    Returns:
        Rebuilt set of message IDs.
    """
    log.info("Rebuilding message index from message files...")
    index = scan_message_files(messages_dir)
    log.info("Rebuilt message index with %d entries", len(index))
    return index


def recover_index(
    index_file: Path,
    messages_dir: Path,
) -> tuple[Set[str], RecoveryResult]:
    """
    Recover a corrupted message index.

    Extracts valid IDs from the corrupted file, then merges with
    IDs rebuilt from message files.

    Returns:
        Tuple of (final_ids, RecoveryResult).
    """
    log.info("Starting message index recovery...")

    preserved_ids: Set[str] = set()
    errors: List[str] = []
    warnings: List[str] = []

    # Extract whatever we can from the corrupted file
    try:
        content = index_file.read_text(encoding="utf-8")
        preserved_ids = extract_valid_ids(content)
        if preserved_ids:
            log.info(
                "Preserved %d valid entries from corrupted index",
                len(preserved_ids),
            )
        else:
            warnings.append("No valid entries could be extracted from corrupted index")
    except OSError as exc:
        warnings.append(f"Failed to read corrupted index: {exc}")

    # Rebuild from message files
    rebuilt_ids = scan_message_files(messages_dir)

    # Merge
    final_ids = preserved_ids | rebuilt_ids
    new_count = len(final_ids - preserved_ids)

    log.info(
        "Recovery complete: %d preserved, %d rebuilt from files, %d total",
        len(preserved_ids),
        new_count,
        len(final_ids),
    )

    result = RecoveryResult(
        recovered=True,
        preserved_count=len(preserved_ids),
        rebuilt_count=new_count,
        total_count=len(final_ids),
        errors=errors,
        warnings=warnings,
    )

    return final_ids, result
