"""
db/write_journal.py — Crash-recovery journal for in-flight debounced writes.

Before a debounced write is scheduled, a journal entry is recorded.  After
the write lands successfully, the entry is removed.  On startup, any stale
entries indicate writes that were in-flight when the process crashed and are
replayed automatically.

The journal is a simple JSONL file in ``workspace/.data/`` — one entry per
line, each a JSON object with ``chat_id``, ``data_hash``, and ``timestamp``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

from src.constants import WORKSPACE_DIR

log = logging.getLogger(__name__)

JOURNAL_FILENAME = "write_journal.jsonl"


def _journal_path() -> Path:
    return Path(WORKSPACE_DIR) / ".data" / JOURNAL_FILENAME


def _hash_data(data: Any) -> str:
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _ensure_dir() -> None:
    path = _journal_path()
    path.parent.mkdir(parents=True, exist_ok=True)


def write_entry(chat_id: str, data: Any) -> str:
    """Record a journal entry before a debounced write. Returns entry ID."""
    _ensure_dir()
    data_hash = _hash_data(data)
    entry = {
        "id": f"{chat_id}:{data_hash}:{int(time.time() * 1000)}",
        "chat_id": chat_id,
        "data_hash": data_hash,
        "timestamp": time.time(),
    }
    path = _journal_path()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log.debug("Journal entry written: %s", entry["id"])
    return entry["id"]


def remove_entry(entry_id: str) -> None:
    """Remove a journal entry after successful write."""
    path = _journal_path()
    if not path.exists():
        return
    lines: list[str] = []
    removed = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == entry_id:
                removed = True
                continue
            lines.append(line)
    if removed:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n" if lines else "")
        log.debug("Journal entry removed: %s", entry_id)


def read_stale_entries() -> list[dict[str, Any]]:
    """Read all journal entries (stale from a previous crash)."""
    path = _journal_path()
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def clear_journal() -> None:
    """Remove the journal file entirely (after replay completes)."""
    path = _journal_path()
    if path.exists():
        path.unlink()
        log.info("Write journal cleared")
