"""
src/workspace_integrity.py — Startup workspace integrity verification.

Runs before component initialization in ``build_bot()`` to detect and
auto-repair common workspace issues: stale temp files, corrupt JSONL
databases, and locked SQLite files.

Usage::

    from src.workspace_integrity import check_workspace_integrity

    result = await check_workspace_integrity(workspace)
    if result.warnings:
        log.warning("Workspace issues: %s", result.warnings)
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field

from src.constants import WORKSPACE_STALE_TEMP_MAX_AGE_HOURS
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IntegrityResult:
    """Result of the startup workspace integrity check."""

    warnings: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_issues(self) -> bool:
        return bool(self.warnings or self.errors)


def _check_data_dir(data_dir: Path, result: IntegrityResult) -> None:
    """Verify .data/ exists and is writable."""
    if not data_dir.exists():
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            result.repaired.append(f"Created missing directory: {data_dir}")
            log.info("Created missing data directory: %s", data_dir)
        except OSError as exc:
            result.errors.append(f"Cannot create data directory {data_dir}: {exc}")
            return

    if not os.access(data_dir, os.W_OK):
        result.errors.append(f"Data directory is not writable: {data_dir}")


def _clean_stale_temps(workspace: Path, result: IntegrityResult) -> None:
    """Remove .tmp files older than the configured threshold."""
    cutoff = time.time() - (WORKSPACE_STALE_TEMP_MAX_AGE_HOURS * 3600)
    count = 0

    try:
        for entry in workspace.rglob("*.tmp"):
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                try:
                    entry.unlink()
                    count += 1
                    log.debug("Removed stale temp: %s", entry)
                except OSError as exc:
                    result.warnings.append(f"Failed to remove stale temp {entry}: {exc}")
    except OSError as exc:
        result.warnings.append(f"Error scanning for temp files: {exc}")

    if count > 0:
        result.repaired.append(f"Removed {count} stale .tmp file(s)")
        log.info("Removed %d stale .tmp file(s)", count)


def _repair_jsonl_last_line(filepath: Path) -> bool:
    """Remove a corrupt last line from a JSONL file.

    Returns True if repaired, False on failure.
    """
    try:
        text = filepath.read_text(encoding="utf-8")
        lines = text.splitlines()
        # Remove trailing corrupt line(s) until we find a valid JSON line or empty
        while lines and lines[-1].strip():
            try:
                json.loads(lines[-1])
                break  # Last line is valid — no repair needed
            except json.JSONDecodeError:
                lines.pop()
        if not lines:
            return False
        filepath.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def _spot_check_jsonl(messages_dir: Path, result: IntegrityResult) -> None:
    """Spot-check first and last line of JSONL files for parseability.

    Auto-repairs corrupt last lines (typically from truncated writes during
    crashes). First-line corruption is flagged as an error since it may
    indicate deeper file damage.
    """
    if not messages_dir.exists():
        return

    corrupted: list[str] = []
    repaired: list[str] = []
    checked = 0

    try:
        jsonl_files = sorted(messages_dir.glob("*.jsonl"))
    except OSError:
        return

    for jsonl_file in jsonl_files:
        try:
            size = jsonl_file.stat().st_size
            if size == 0:
                continue

            with jsonl_file.open("r", encoding="utf-8") as f:
                # Check first line
                first_line = f.readline()
                if first_line.strip():
                    try:
                        json.loads(first_line)
                    except json.JSONDecodeError:
                        corrupted.append(f"{jsonl_file.name}:1")
                        continue

                # For last line, seek to end and read backwards
                last_corrupt = False
                if size > 1024:
                    f.seek(max(0, size - 1024))
                    tail = f.read()
                    lines = tail.splitlines()
                    if lines:
                        last_line = lines[-1]
                        if last_line.strip():
                            try:
                                json.loads(last_line)
                            except json.JSONDecodeError:
                                last_corrupt = True
                elif size > len(first_line):
                    # Small file — just read all and check last
                    remaining = f.read()
                    lines = remaining.splitlines()
                    if lines and lines[-1].strip():
                        try:
                            json.loads(lines[-1])
                        except json.JSONDecodeError:
                            last_corrupt = True

                if last_corrupt:
                    if _repair_jsonl_last_line(jsonl_file):
                        repaired.append(jsonl_file.name)
                    else:
                        corrupted.append(f"{jsonl_file.name}:(last)")

                checked += 1
                if checked >= 20:
                    break

        except OSError as exc:
            result.warnings.append(f"Cannot read {jsonl_file.name}: {exc}")

    if corrupted:
        result.warnings.append(f"Corrupt JSONL detected (first/last line): {corrupted[:5]}")
        log.warning(
            "Startup integrity: corrupt JSONL files detected: %s",
            corrupted,
        )
    if repaired:
        result.repaired.append(f"Auto-repaired corrupt last line in: {repaired[:5]}")
        log.info("Startup integrity: auto-repaired %d JSONL file(s): %s", len(repaired), repaired)


def _check_sqlite_not_locked(db_path: Path, result: IntegrityResult) -> None:
    """Try opening a SQLite DB read-only to detect lock conflicts."""
    if not db_path.exists():
        return

    uri = f"file:{db_path}?mode=ro&nolock=1"
    try:
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            # Set WAL mode even on read-only connections so the shared-cache
            # layer uses WAL locking semantics (prevents accidental rollback
            # journal fallbacks when the main writer has WAL enabled).
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("SELECT 1")
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            result.warnings.append(f"SQLite database may be locked: {db_path.name} ({exc})")
            log.warning(
                "Startup integrity: SQLite DB may be locked: %s — %s",
                db_path.name,
                exc,
            )
    except Exception as exc:
        result.warnings.append(f"Cannot verify SQLite DB {db_path.name}: {exc}")


def _run_sync_checks(workspace: Path) -> IntegrityResult:
    """Execute all synchronous integrity checks. Returns the combined result."""
    result = IntegrityResult()

    data_dir = workspace / ".data"
    messages_dir = data_dir / "messages"

    _check_data_dir(data_dir, result)
    _clean_stale_temps(workspace, result)
    _spot_check_jsonl(messages_dir, result)
    _check_sqlite_not_locked(workspace / ".data" / "vector_memory.db", result)
    _check_sqlite_not_locked(workspace / ".data" / "projects.db", result)

    return result


async def check_workspace_integrity(workspace: Path) -> IntegrityResult:
    """Run startup workspace integrity checks with auto-repair.

    Verifies: data dir accessibility, stale temp cleanup, JSONL
    spot-checks, and SQLite lock detection.  All I/O runs in a
    thread pool to avoid blocking the event loop.

    Args:
        workspace: Root workspace directory (e.g. ``Path("workspace")``).

    Returns:
        IntegrityResult with any warnings, repairs, or errors found.
    """
    import asyncio

    result = await asyncio.to_thread(_run_sync_checks, workspace)

    if result.has_issues:
        log.warning(
            "Workspace integrity check found issues: %d warnings, %d errors, %d auto-repaired",
            len(result.warnings),
            len(result.errors),
            len(result.repaired),
        )
    else:
        log.debug("Workspace integrity check passed")

    return result
