"""
src/db/db_integrity.py — Corruption detection and repair for message files.

Standalone functions that operate on message file paths. Called by
Database thin wrappers to keep db.py focused on core CRUD operations.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.db.db_utils import _validate_chat_id
from src.utils import JSONDecodeError, json_loads

log = logging.getLogger(__name__)


# ── dataclasses ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class CorruptionResult:
    """Result of message file corruption detection."""

    file_path: str  # Path to the file checked
    is_corrupted: bool  # Whether corruption was detected
    corrupted_lines: List[int] = field(default_factory=list)
    checksum_mismatches: List[int] = field(default_factory=list)
    total_lines: int = 0
    valid_lines: int = 0
    error_details: List[str] = field(default_factory=list)
    backup_path: Optional[str] = None
    repaired: bool = False


@dataclass(slots=True)
class MessageLine:
    """Parsed message line with checksum validation."""

    id: str
    role: str
    content: str
    name: Optional[str]
    timestamp: float
    checksum: Optional[str]
    line_number: int
    raw_line: str
    is_valid: bool = True
    validation_error: Optional[str] = None


# ── checksum helpers ────────────────────────────────────────────────────────


def calculate_checksum(content: str, role: str, timestamp: float) -> str:
    """
    Calculate SHA256 checksum for message content.

    Returns:
        Hexadecimal checksum string (first 16 chars).
    """
    data = f"{role}:{timestamp}:{content}"
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:16]


def validate_checksum(msg: dict) -> Tuple[bool, Optional[str]]:
    """
    Validate message checksum.

    Returns:
        Tuple of (is_valid, error_message).
    """
    checksum = msg.get("_checksum")
    if not checksum:
        return True, None  # Legacy messages without checksum are valid

    content = msg.get("content", "")
    role = msg.get("role", "")
    timestamp = msg.get("timestamp", 0)

    expected = calculate_checksum(content, role, timestamp)
    if checksum != expected:
        return False, f"Checksum mismatch: expected {expected}, got {checksum}"
    return True, None


# ── detection ───────────────────────────────────────────────────────────────


def detect_corruption_sync(msg_file: Path) -> CorruptionResult:
    """
    Detect corruption in a message file (synchronous).

    Validates JSON format and checksum integrity for every line.
    """
    result = CorruptionResult(file_path=str(msg_file), is_corrupted=False)

    if not msg_file.exists():
        result.error_details.append(f"File does not exist: {msg_file}")
        return result

    try:
        content = msg_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        result.total_lines = len([ln for ln in lines if ln.strip()])

        for line_num, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                msg = json_loads(line)
                result.valid_lines += 1

                is_valid, error = validate_checksum(msg)
                if not is_valid:
                    result.checksum_mismatches.append(line_num)
                    result.error_details.append(f"Line {line_num}: {error}")
            except JSONDecodeError as exc:
                result.corrupted_lines.append(line_num)
                result.error_details.append(f"Line {line_num}: JSON parse error - {exc}")

        result.is_corrupted = bool(result.corrupted_lines or result.checksum_mismatches)

        if result.is_corrupted:
            log.warning(
                "Corruption detected in %s: %d corrupted lines, %d checksum mismatches",
                msg_file.name,
                len(result.corrupted_lines),
                len(result.checksum_mismatches),
            )

    except OSError as exc:
        result.is_corrupted = True
        result.error_details.append(f"Failed to read file: {exc}")
        log.error("Failed to read message file %s: %s", msg_file.name, exc)

    return result


# ── backup ──────────────────────────────────────────────────────────────────


def backup_file_sync(msg_file: Path, data_dir: Path) -> Optional[str]:
    """
    Create a timestamped backup of a message file.

    Returns:
        Path to backup file, or None if backup failed.
    """
    if not msg_file.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_file = backup_dir / f"{msg_file.stem}_{timestamp}.bak"

    try:
        shutil.copy2(msg_file, backup_file)
        log.info("Created backup: %s", backup_file)
        return str(backup_file)
    except OSError as exc:
        log.error("Failed to create backup: %s", exc)
        return None


# ── repair ──────────────────────────────────────────────────────────────────


def repair_file_sync(
    msg_file: Path,
    detection_result: CorruptionResult,
    atomic_write_fn,
) -> bool:
    """
    Remove corrupted / checksum-mismatched lines from a message file.

    Args:
        msg_file: Path to message file.
        detection_result: Result from detect_corruption_sync.
        atomic_write_fn: Callable(path, content) for atomic writes.

    Returns:
        True if repair succeeded.
    """
    if not msg_file.exists():
        return False

    skip_lines = set(detection_result.corrupted_lines) | set(detection_result.checksum_mismatches)
    if not skip_lines:
        return True  # Nothing to repair

    try:
        content = msg_file.read_text(encoding="utf-8")
        lines = content.splitlines()

        valid_lines = [line for line_num, line in enumerate(lines, 1) if line_num not in skip_lines]

        new_content = "\n".join(valid_lines)
        if valid_lines:
            new_content += "\n"

        atomic_write_fn(msg_file, new_content)

        log.info(
            "Repaired %s: removed %d corrupted lines",
            msg_file.name,
            len(skip_lines),
        )
        return True

    except OSError as exc:
        log.error("Failed to repair file %s: %s", msg_file.name, exc)
        return False


# ── batch validation ────────────────────────────────────────────────────────


def validate_all_sync(
    messages_dir: Path,
) -> Dict[str, CorruptionResult]:
    """
    Validate all .jsonl message files in a directory (detection only).

    Returns:
        Dict mapping chat_id (stem) to CorruptionResult.
    """
    results: Dict[str, CorruptionResult] = {}

    if not messages_dir.exists():
        return results

    for msg_file in messages_dir.glob("*.jsonl"):
        chat_id = msg_file.stem
        try:
            _validate_chat_id(chat_id)
        except ValueError:
            log.warning(
                "Skipping message file with invalid chat_id stem: %s",
                msg_file.name,
            )
            continue
        result = detect_corruption_sync(msg_file)
        results[chat_id] = result

        if result.is_corrupted:
            log.warning(
                "Corruption in chat %s: %d corrupted, %d checksum errors",
                chat_id,
                len(result.corrupted_lines),
                len(result.checksum_mismatches),
            )

    return results
