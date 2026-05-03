"""
message_queue_persistence.py — JSONL persistence layer for the message queue.

Handles all file I/O for the message queue: reading, writing, buffering,
crash recovery, and integrity validation.  Extracted from ``message_queue.py``
to isolate persistence concerns and make them independently testable.

All methods are synchronous — callers wrap in ``asyncio.to_thread()`` as
needed.  This mirrors the ``db.py → message_store.py`` split from Round 3.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from src.constants import QUEUE_FSYNC_BATCH_SIZE, QUEUE_FSYNC_INTERVAL_SECONDS
from src.core.errors import NonCriticalCategory, log_noncritical
from src.utils import JsonParseMode, json_dumps, safe_json_parse

if TYPE_CHECKING:
    from src.message_queue import MessageStatus, QueueCorruptionResult, QueuedMessage

log = logging.getLogger(__name__)

__all__ = ["QueuePersistence"]


class QueuePersistence:
    """Synchronous JSONL persistence layer for the message queue.

    Manages the write buffer, atomic file rewrites, crash recovery from
    orphaned temp files, and integrity validation/repair.  All methods are
    synchronous; callers wrap in ``asyncio.to_thread()`` as needed.
    """

    def __init__(self, queue_file: Path) -> None:
        self._queue_file = queue_file
        self._dir = queue_file.parent

        # ── batched fsync state ───────────────────────────────────────────
        # Accumulates pending lines for write coalescing.  The buffer is
        # flushed when: (1) batch size reached, (2) interval elapsed,
        # (3) close() / persist_messages() called.
        self._write_buffer: List[str] = []
        self._fsync_batch_size: int = QUEUE_FSYNC_BATCH_SIZE
        self._fsync_interval: float = QUEUE_FSYNC_INTERVAL_SECONDS
        self._last_flush_time: float = 0.0
        # Safety cap: if the write buffer grows beyond this size, force a
        # flush regardless of batch/interval thresholds.
        self._write_buffer_max_size: int = QUEUE_FSYNC_BATCH_SIZE * 10

    # ── properties ────────────────────────────────────────────────────────

    @property
    def queue_file(self) -> Path:
        """Path to the queue JSONL file."""
        return self._queue_file

    @property
    def write_buffer(self) -> List[str]:
        """Current write buffer contents (read-only view of internal list)."""
        return self._write_buffer

    @property
    def fsync_interval(self) -> float:
        """Configured interval between timed flushes, in seconds."""
        return self._fsync_interval

    # ── buffer management ─────────────────────────────────────────────────

    def buffer_line(self, line: str) -> None:
        """Append a line to the write buffer."""
        self._write_buffer.append(line)

    def should_flush(self) -> bool:
        """Check if the write buffer meets any flush threshold."""
        now = time.monotonic()
        size_reached = len(self._write_buffer) >= self._fsync_batch_size
        interval_reached = (
            bool(self._write_buffer)
            and (now - self._last_flush_time) >= self._fsync_interval
        )
        max_reached = len(self._write_buffer) >= self._write_buffer_max_size
        return size_reached or interval_reached or max_reached

    def swap_buffer(self) -> List[str]:
        """Atomically detach and return the current write buffer.

        Returns the current buffer list and replaces it with a fresh empty
        list.  Callers can then flush the detached lines to disk *without*
        holding the main lock, allowing concurrent ``buffer_line()`` calls
        to proceed against the new buffer.
        """
        lines = self._write_buffer
        self._write_buffer = []
        return lines

    def flush_lines(self, lines: List[str]) -> None:
        """Flush a pre-detached list of lines to disk with a single fsync.

        Used after ``swap_buffer()`` to write the detached batch without
        interacting with the live buffer.
        """
        if not lines:
            return
        try:
            with self._queue_file.open("a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
                os.fsync(f.fileno())
            self._last_flush_time = time.monotonic()
        except Exception as exc:
            log.error("Failed to flush write buffer: %s", exc)
            raise

    def flush_buffer(self) -> None:
        """Flush all buffered lines to disk with a single fsync.

        Swaps the buffer atomically so that concurrent buffer_line() calls
        (if any slip through before the lock is acquired) write to a fresh
        list rather than corrupting the in-flight batch.
        """
        if not self._write_buffer:
            return
        lines = self.swap_buffer()

        try:
            with self._queue_file.open("a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
                os.fsync(f.fileno())
            self._last_flush_time = time.monotonic()
        except Exception as exc:
            log.error("Failed to flush write buffer: %s", exc)
            raise

    def clear_buffer(self) -> None:
        """Clear the write buffer without flushing to disk."""
        self._write_buffer.clear()

    # ── full-file persistence ─────────────────────────────────────────────

    def persist_messages(self, messages: List[QueuedMessage]) -> None:
        """Atomically persist all messages to the queue file.

        Drains the write buffer first (buffered lines would be overwritten
        by the atomic write), then rewrites the file with only the given
        messages using the temp-file-then-rename pattern.
        """
        from src.utils.async_file import sync_atomic_write

        # Drain buffer before full rewrite — buffered lines would be
        # overwritten by the atomic write otherwise.
        self.flush_buffer()

        content = "".join(
            json_dumps(msg.to_dict(), ensure_ascii=False) + "\n" for msg in messages
        )
        sync_atomic_write(self._queue_file, content)

    def cleanup_temp_file(self) -> None:
        """Remove the temp file if it exists (after failed persist)."""
        tmp = self._queue_file.with_suffix(".tmp")
        if tmp.exists():
            tmp.unlink()

    # ── crash recovery ────────────────────────────────────────────────────

    def promote_orphaned_tmp(self) -> None:
        """Promote orphaned .tmp file to main queue file when main is missing.

        If the process crashed during ``persist_messages()``'s atomic write
        (specifically between unlink and rename on Windows), the main file
        is deleted and only the .tmp remains.  This method promotes the .tmp
        file to restore the queue state.  If both files exist, the orphaned
        .tmp is cleaned up (main is authoritative in that case).
        """
        tmp_file = self._queue_file.with_suffix(".tmp")
        if not tmp_file.exists():
            return

        if not self._queue_file.exists():
            # Crash between unlink and rename — .tmp is the intended state
            try:
                tmp_file.rename(self._queue_file)
                log.warning("Promoted orphaned temp file to queue file")
            except Exception as exc:
                log.error("Failed to promote temp file: %s", exc)
        else:
            # Both exist — crash before the unlink step; main is authoritative
            try:
                tmp_file.unlink()
                log.info("Removed orphaned temp file (main file is authoritative)")
            except Exception as exc:
                log.warning("Failed to remove orphaned temp file: %s", exc)

    def backup_corrupted_file(self) -> None:
        """Create a timestamped backup of the queue file before eviction.

        Follows the same pattern as ``db_integrity.backup_file_sync``:
        copies the corrupted file to ``<data_dir>/backups/`` with a
        timestamped name so that a sysadmin can manually inspect or
        recover data that was skipped during line-level parsing.
        """
        if not self._queue_file.exists():
            return

        backup_dir = self._dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = backup_dir / f"message_queue_{timestamp}.jsonl.bak"

        try:
            shutil.copy2(self._queue_file, backup_path)
            log.info("Backed up corrupted queue file to %s", backup_path)
        except Exception as exc:
            log.warning("Failed to backup corrupted queue file: %s", exc)

    # ── load ──────────────────────────────────────────────────────────────

    def load_pending(self) -> Tuple[Dict[str, QueuedMessage], Optional[QueueCorruptionResult]]:
        """Load pending messages from the queue file.

        Performs crash recovery (orphaned .tmp promotion), streams the
        JSONL file line-by-line loading only pending messages, and
        optionally backs up and evicts non-pending entries.

        Returns:
            Tuple of (pending messages dict, optional corruption result).
        """
        from src.message_queue import MessageStatus, QueueCorruptionResult, QueuedMessage

        # Recover from crashed atomic write before checking main file
        self.promote_orphaned_tmp()

        if not self._queue_file.exists():
            log.debug("Queue file does not exist, starting fresh")
            return {}, None

        total_lines = 0
        loaded_count = 0
        pending_count = 0
        completed_count = 0
        corrupted_count = 0
        corrupted_line_numbers: List[int] = []
        error_details: List[str] = []
        pending: Dict[str, QueuedMessage] = {}

        try:
            with self._queue_file.open("r", encoding="utf-8", errors="replace") as fh:
                for line_idx, line in enumerate(fh, start=1):
                    line = line.rstrip("\n\r")
                    total_lines += 1

                    data = safe_json_parse(
                        line, default=None, log_errors=True, mode=JsonParseMode.LINE
                    )
                    if data is None:
                        corrupted_count += 1
                        corrupted_line_numbers.append(line_idx)
                        error_details.append(f"Line {line_idx}: JSON parse failed")
                        continue

                    if data.get("status") == "completed":
                        completed_count += 1
                        loaded_count += 1
                        continue

                    try:
                        msg = QueuedMessage.from_dict(data)
                        loaded_count += 1
                        pending[msg.message_id] = msg
                        pending_count += 1
                    except (KeyError, ValueError) as exc:
                        corrupted_count += 1
                        corrupted_line_numbers.append(line_idx)
                        error_details.append(f"Line {line_idx}: {str(exc)[:80]}")
                        continue
        except Exception as exc:
            log.error("Failed to read queue file: %s", exc)
            return {}, None

        is_corrupted = corrupted_count > 0

        corruption_result = QueueCorruptionResult(
            file_path=str(self._queue_file),
            is_corrupted=is_corrupted,
            corrupted_lines=corrupted_line_numbers,
            total_lines=total_lines,
            valid_lines=loaded_count,
            pending_lines=pending_count,
            completed_lines=completed_count,
            error_details=error_details,
        )

        if is_corrupted:
            log.warning(
                "Queue file: recovered %d valid entries, skipped %d corrupted out of %d lines",
                loaded_count,
                corrupted_count,
                total_lines,
            )
            self.backup_corrupted_file()

        log.info(
            "Loaded queue file: %d entries, %d pending",
            loaded_count,
            pending_count,
        )

        # Eager eviction: rewrite file with only pending entries
        evicted = total_lines - pending_count
        if evicted > 0:
            try:
                self.persist_messages(list(pending.values()))
                log.info("Evicted %d non-pending entries from queue file", evicted)
            except Exception as exc:
                log.warning("Failed to evict non-pending entries: %s", exc)

        return pending, corruption_result

    # ── integrity ─────────────────────────────────────────────────────────

    def validate_sync(self) -> QueueCorruptionResult:
        """Synchronous integrity validation of the queue file.

        Reads the on-disk JSONL file and validates each line without
        modifying the in-memory pending index or rewriting the file.
        """
        from src.message_queue import MessageStatus, QueueCorruptionResult, QueuedMessage

        result = QueueCorruptionResult(
            file_path=str(self._queue_file),
            is_corrupted=False,
        )

        if not self._queue_file.exists():
            result.error_details.append("Queue file does not exist")
            return result

        try:
            content = self._queue_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            result.is_corrupted = True
            result.error_details.append(f"Failed to read file: {exc}")
            return result

        for line_num, line in enumerate(content.splitlines(), start=1):
            if not line.strip():
                continue
            result.total_lines += 1

            data = safe_json_parse(
                line, default=None, log_errors=True, mode=JsonParseMode.LINE
            )
            if data is None:
                result.corrupted_lines.append(line_num)
                result.error_details.append(f"Line {line_num}: JSON parse failed")
                continue

            try:
                msg = QueuedMessage.from_dict(data)
                result.valid_lines += 1
                if msg.status == MessageStatus.PENDING:
                    result.pending_lines += 1
                else:
                    result.completed_lines += 1
            except (KeyError, ValueError) as exc:
                result.corrupted_lines.append(line_num)
                result.error_details.append(f"Line {line_num}: {str(exc)[:80]}")

        result.is_corrupted = bool(result.corrupted_lines)
        return result

    def repair_sync(self, detection: QueueCorruptionResult) -> None:
        """Remove corrupted lines from the queue file (synchronous).

        Reads the file, filters out lines identified in the detection
        result, and atomically rewrites with only valid content.
        """
        from src.utils.async_file import sync_atomic_write

        skip = set(detection.corrupted_lines)
        if not skip:
            return

        content = self._queue_file.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        valid_lines = [ln for num, ln in enumerate(lines, 1) if num not in skip]

        new_content = "\n".join(valid_lines)
        if valid_lines:
            new_content += "\n"

        sync_atomic_write(self._queue_file, new_content)

    def load_valid_lines_sync(self) -> Dict[str, QueuedMessage]:
        """Load valid pending entries from queue file.

        Called after repair to repopulate the pending index from the
        cleaned file.

        Returns:
            Dict of ``message_id`` → ``QueuedMessage`` for pending entries.
        """
        from src.message_queue import MessageStatus, QueuedMessage

        pending: Dict[str, QueuedMessage] = {}

        if not self._queue_file.exists():
            return pending

        try:
            content = self._queue_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return pending

        for line in content.splitlines():
            data = safe_json_parse(
                line, default=None, log_errors=True, mode=JsonParseMode.LINE
            )
            if data is None:
                continue
            try:
                msg = QueuedMessage.from_dict(data)
                if msg.status == MessageStatus.PENDING:
                    pending[msg.message_id] = msg
            except (KeyError, ValueError):
                continue

        return pending
