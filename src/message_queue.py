"""
message_queue.py — Persistent message queue for crash recovery.

Provides a persistent queue that tracks in-flight messages, enabling
recovery after crashes or restarts. Messages are marked as pending
before processing and completed after successful handling.

Storage structure:
    .data/
    └── message_queue.jsonl   # Pending messages (JSONL = one JSON per line)

Queue states:
    - pending: Message is being processed
    - completed: Message was successfully processed
    - stale: Message timed out (crash recovery candidate)

Lock model: Uses AsyncLock (from src.utils.locking) for file I/O (same pattern as db.py).
AsyncLock provides lazy-initialised asyncio.Lock that is safe to create before the
event loop is running (Python 3.10+ / Windows ProactorEventLoop compatibility).
All queue operations are async and wrapped in await asyncio.to_thread() for the
actual disk writes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.constants import MAX_QUEUED_TEXT_LENGTH, QUEUE_FSYNC_BATCH_SIZE, QUEUE_FSYNC_INTERVAL_SECONDS
from src.core.errors import NonCriticalCategory, log_noncritical
from src.db.db_utils import _validate_chat_id
from src.utils import JsonParseMode, json_dumps, safe_json_parse
from src.utils.locking import AsyncLockMixin

if TYPE_CHECKING:
    from src.channels.base import IncomingMessage

log = logging.getLogger(__name__)


class MessageStatus(str, Enum):
    """Status of a queued message."""

    PENDING = "pending"
    COMPLETED = "completed"


@dataclass(slots=True)
class QueuedMessage:
    """
    A message in the persistence queue.

    Attributes:
        message_id: Unique message identifier
        chat_id: Chat/group ID
        text: Message content
        sender_id: Sender's phone/user ID
        sender_name: Optional sender name
        channel: Source channel identifier
        metadata: Additional message metadata
        status: Current processing status
        created_at: Timestamp when message was queued
        updated_at: Timestamp of last status update
    """

    message_id: str
    chat_id: str
    text: str
    sender_id: Optional[str] = None
    sender_name: Optional[str] = None
    channel: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    status: MessageStatus = MessageStatus.PENDING
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "message_id": self.message_id,
            "chat_id": self.chat_id,
            "text": self.text,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "channel": self.channel,
            "metadata": self.metadata,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueuedMessage":
        """Create from dictionary (JSON deserialization).

        Validates ``chat_id`` at the deserialization boundary to prevent
        malicious values loaded from the on-disk queue file from reaching
        filesystem operations downstream.  This is the defense-in-depth
        layer between disk persistence and IncomingMessage construction
        during crash recovery.
        """
        _validate_chat_id(data["chat_id"])
        return cls(
            message_id=data["message_id"],
            chat_id=data["chat_id"],
            text=data["text"],
            sender_id=data.get("sender_id"),
            sender_name=data.get("sender_name"),
            channel=data.get("channel"),
            metadata=data.get("metadata", {}),
            status=MessageStatus(data.get("status", "pending")),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )

    @classmethod
    def from_incoming_message(cls, msg: "IncomingMessage") -> "QueuedMessage":
        """Create from an IncomingMessage."""
        return cls(
            message_id=msg.message_id,
            chat_id=msg.chat_id,
            text=msg.text,
            sender_id=msg.sender_id,
            sender_name=msg.sender_name,
            channel=msg.channel_type,
            metadata={},  # IncomingMessage has no metadata attribute
        )


@dataclass(slots=True)
class QueueCorruptionResult:
    """Result of queue file corruption detection and repair.

    Follows the same pattern as db_integrity.CorruptionResult but
    adapted for the queue's JSONL format and message status model.

    Attributes:
        file_path: Path to the queue file checked.
        is_corrupted: Whether any corruption was detected.
        corrupted_lines: 1-indexed line numbers that failed to parse.
        total_lines: Total non-empty lines in the file.
        valid_lines: Lines that parsed as valid QueuedMessage dicts.
        pending_lines: Lines representing pending (active) messages.
        completed_lines: Lines representing completed/evicted messages.
        error_details: Human-readable error descriptions per corrupted line.
        backup_path: Path to the backup file, if one was created.
        repaired: Whether a repair was performed.
    """

    file_path: str
    is_corrupted: bool
    corrupted_lines: List[int] = field(default_factory=list)
    total_lines: int = 0
    valid_lines: int = 0
    pending_lines: int = 0
    completed_lines: int = 0
    error_details: List[str] = field(default_factory=list)
    backup_path: Optional[str] = None
    repaired: bool = False


class MessageQueue(AsyncLockMixin):
    """
    Persistent message queue for crash recovery.

    Tracks messages through their processing lifecycle:
    1. enqueue() - Mark message as pending before processing
    2. complete() - Mark message as completed after success
    3. recover_stale() - Find and return timed-out pending messages

    Thread-safe via asyncio locks. Uses atomic writes for safety.

    Example:
        queue = MessageQueue(".data")

        # Before processing
        await queue.enqueue(message)

        try:
            result = await process(message)
            await queue.complete(message.message_id)
        except Exception:
            # Message stays pending for crash recovery
            raise

        # On startup, recover any stale messages
        stale = await queue.recover_stale(timeout_seconds=300)
        for msg in stale:
            await process(msg)
    """

    DEFAULT_STALE_TIMEOUT = 300  # 5 minutes

    def __init__(self, data_dir: str, stale_timeout: int = DEFAULT_STALE_TIMEOUT) -> None:
        """
        Initialize message queue.

        Args:
            data_dir: Path to data directory for queue storage.
            stale_timeout: Seconds after which a pending message is considered stale.
        """
        super().__init__()
        self._dir = Path(data_dir)
        self._queue_file = self._dir / "message_queue.jsonl"
        self._stale_timeout = stale_timeout

        # In-memory index of pending messages (message_id -> QueuedMessage)
        self._pending: Dict[str, QueuedMessage] = {}

        # Track completed IDs for append-only writes with periodic compaction
        self._completed_since_compact: int = 0
        self._compact_threshold: int = 20  # compact after this many completions

        self._initialized = False

        # Populated after connect() / _load_pending() with structured
        # corruption metadata for observability and health checks.
        self._last_corruption_result: Optional[QueueCorruptionResult] = None

        # ── batched fsync state ───────────────────────────────────────────
        # Accumulates pending lines for write coalescing.  The buffer is
        # flushed when: (1) batch size reached, (2) interval elapsed,
        # (3) close() / _persist_pending() called.
        self._write_buffer: List[str] = []
        self._fsync_batch_size: int = QUEUE_FSYNC_BATCH_SIZE
        self._fsync_interval: float = QUEUE_FSYNC_INTERVAL_SECONDS
        self._last_flush_time: float = 0.0

        # Background task that periodically flushes the write buffer when
        # the time-interval threshold is reached but no new enqueue calls
        # arrive.  Ensures buffered writes are persisted within the
        # configured interval even during idle periods.
        self._flush_task: Optional[asyncio.Task[None]] = None

    async def connect(self) -> None:
        """
        Initialize queue storage and load pending messages.

        Creates the data directory if needed and loads any pending
        messages from disk into memory.

        Side Effects:
            - Creates .data/ directory if missing
            - Loads pending messages into _pending cache
            - Sets _initialized flag to True
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        await self._load_pending()

        self._initialized = True

        # Start background flush loop to drain buffered writes on the
        # time-interval boundary during idle periods.
        self._flush_task = asyncio.create_task(self._flush_loop())

        log.info(
            "Message queue initialized with %d pending messages",
            len(self._pending),
        )

    async def close(self) -> None:
        """
        Flush pending writes and close queue.

        Ensures all pending messages are persisted to disk, including
        any buffered writes awaiting batch fsync.

        Side Effects:
            - Cancels background flush task
            - Flushes write buffer to disk
            - Persists pending messages to disk
            - Sets _initialized flag to False
        """
        # Cancel background flush loop first so it doesn't race with
        # the final flush below.
        if self._flush_task is not None:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None

        async with self._lock:
            await self._flush_write_buffer()
            await self._persist_pending()

        self._initialized = False
        log.info("Message queue closed, persisted %d pending messages", len(self._pending))

    async def __aenter__(self) -> "MessageQueue":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    # ── core operations ───────────────────────────────────────────────────────

    async def enqueue(self, msg: "IncomingMessage") -> QueuedMessage:
        """
        Add a message to the queue as pending.

        Must be called before processing to enable crash recovery.
        The message will be persisted to disk atomically.

        Args:
            msg: Incoming message to queue.

        Returns:
            The queued message object.

        Side Effects:
            - Adds message to in-memory pending index
            - Appends to queue file atomically
        """
        queued = QueuedMessage.from_incoming_message(msg)

        # Truncate excessively long text before persisting to the queue file.
        # The full text is still passed through to the bot for normal
        # processing — only the queue copy is capped to prevent the JSONL
        # file from growing unboundedly.
        if len(queued.text) > MAX_QUEUED_TEXT_LENGTH:
            original_len = len(queued.text)
            suffix = "…[truncated]"
            queued.text = queued.text[: MAX_QUEUED_TEXT_LENGTH - len(suffix)] + suffix
            log.warning(
                "Truncated queued message %s text: %d → %d chars",
                queued.message_id,
                original_len,
                len(queued.text),
            )

        async with self._lock:
            self._pending[queued.message_id] = queued
            await self._append_to_queue(queued)

        log.debug(
            "Enqueued message %s for chat %s",
            queued.message_id,
            queued.chat_id,
        )
        return queued

    async def complete(self, message_id: str) -> bool:
        """
        Mark a message as completed and remove from pending.

        Should be called after successful message processing.
        Uses append-only write (appends COMPLETED entry) and only
        rewrites the full file when the compaction threshold is reached.

        Args:
            message_id: ID of the message to complete.

        Returns:
            True if message was found and completed, False otherwise.

        Side Effects:
            - Removes message from in-memory pending index
            - Appends completion marker to queue file
            - Periodically compacts the queue file (full rewrite)
        """
        async with self._lock:
            if message_id not in self._pending:
                log.warning(
                    "Attempted to complete unknown message %s",
                    message_id,
                )
                return False

            del self._pending[message_id]
            self._completed_since_compact += 1

            # Compact (full rewrite) only when threshold is reached
            if self._completed_since_compact >= self._compact_threshold:
                await self._persist_pending()
                self._completed_since_compact = 0
            else:
                # Append-only: write a completion marker
                await self._append_completion(message_id)

        log.debug("Completed message %s", message_id)
        return True

    async def recover_stale(self, timeout_seconds: Optional[int] = None) -> List[QueuedMessage]:
        """
        Find and return stale pending messages for crash recovery.

        Stale messages are pending messages that have exceeded the
        timeout threshold, indicating they may have been interrupted
        by a crash. These messages should be reprocessed.

        Args:
            timeout_seconds: Custom timeout (uses stale_timeout if not provided).

        Returns:
            List of stale messages that need reprocessing.

        Side Effects:
            - Logs recovery operations
            - Updates timestamps for recovered messages
            - Persists updated queue
        """
        timeout = timeout_seconds if timeout_seconds is not None else self._stale_timeout
        cutoff_time = time.time() - timeout
        stale_messages: List[QueuedMessage] = []

        async with self._lock:
            for msg_id, msg in list(self._pending.items()):
                if msg.updated_at < cutoff_time:
                    stale_messages.append(msg)

            if stale_messages:
                log.warning(
                    "Recovering %d stale messages (timeout=%ds)",
                    len(stale_messages),
                    timeout,
                )

                # Log each recovered message for debugging
                for msg in stale_messages:
                    log.info(
                        "Recovering stale message %s from chat %s (age: %.1fs)",
                        msg.message_id,
                        msg.chat_id,
                        time.time() - msg.updated_at,
                    )

                # Remove stale messages from pending (they'll be reprocessed)
                for msg in stale_messages:
                    del self._pending[msg.message_id]

                await self._persist_pending()

        return stale_messages

    async def get_pending_count(self) -> int:
        """
        Get the number of pending messages in the queue.

        Returns:
            Count of pending messages.
        """
        async with self._lock:
            return len(self._pending)

    async def get_pending_for_chat(self, chat_id: str) -> List[QueuedMessage]:
        """
        Get all pending messages for a specific chat.

        Args:
            chat_id: Chat/group ID to filter by.

        Returns:
            List of pending messages for the chat.
        """
        async with self._lock:
            return [msg for msg in self._pending.values() if msg.chat_id == chat_id]

    # ── integrity operations ─────────────────────────────────────────────────

    async def validate(self) -> QueueCorruptionResult:
        """
        Check queue file integrity without loading into memory.

        Reads the on-disk JSONL file and validates each line without
        modifying the in-memory pending index or rewriting the file.
        Safe to call at any time for health checks.

        Returns:
            QueueCorruptionResult with detailed corruption metadata.

        Example:
            result = await queue.validate()
            if result.is_corrupted:
                print(f"Found {len(result.corrupted_lines)} bad lines")
        """
        async with self._lock:
            return await asyncio.to_thread(self._validate_sync)

    def _validate_sync(self) -> QueueCorruptionResult:
        """Synchronous implementation of validate() for to_thread."""
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
                line, default=None, log_errors=False, mode=JsonParseMode.LINE
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

    async def repair(self) -> QueueCorruptionResult:
        """
        Detect and repair corruption in the queue file.

        Validates every line in the queue file, backs up the corrupted
        file, and rewrites it with only valid lines. The in-memory
        pending index is reloaded from the repaired file.

        Returns:
            QueueCorruptionResult with repair details including
            backup_path and repaired flag.

        Side Effects:
            - Creates backup in .data/backups/ before repair
            - Rewrites queue file with only valid lines
            - Reloads _pending from repaired file
        """
        async with self._lock:
            # Step 1: Detect corruption
            result = await asyncio.to_thread(self._validate_sync)

            if not result.is_corrupted:
                return result

            # Step 2: Backup before modifying
            await self._backup_corrupted_file()

            # Record backup path in result
            backup_dir = self._dir / "backups"
            if backup_dir.exists():
                backups = sorted(backup_dir.glob("message_queue_*.jsonl.bak"))
                if backups:
                    result.backup_path = str(backups[-1])

            # Step 3: Rewrite with only valid lines
            await asyncio.to_thread(self._repair_sync, result)
            result.repaired = True

            # Step 4: Reload pending from repaired file
            self._pending.clear()
            self._load_valid_lines_sync()

            # Update stored corruption result
            self._last_corruption_result = result

            log.info(
                "Repaired queue file: removed %d corrupted lines, %d valid lines preserved",
                len(result.corrupted_lines),
                result.valid_lines,
            )
            return result

    def _repair_sync(self, detection: QueueCorruptionResult) -> None:
        """Remove corrupted lines from the queue file (synchronous).

        Reads the file, filters out lines identified in the detection
        result, and atomically rewrites with only valid content.
        """
        skip = set(detection.corrupted_lines)
        if not skip:
            return

        content = self._queue_file.read_text(encoding="utf-8", errors="replace")
        lines = content.splitlines()
        valid_lines = [ln for num, ln in enumerate(lines, 1) if num not in skip]

        new_content = "\n".join(valid_lines)
        if valid_lines:
            new_content += "\n"

        from src.utils.async_file import sync_atomic_write

        sync_atomic_write(self._queue_file, new_content)

    def _load_valid_lines_sync(self) -> None:
        """Load valid pending entries from queue file (synchronous).

        Called after repair to repopulate _pending from the cleaned file.
        """
        if not self._queue_file.exists():
            return

        try:
            content = self._queue_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return

        for line in content.splitlines():
            data = safe_json_parse(
                line, default=None, log_errors=False, mode=JsonParseMode.LINE
            )
            if data is None:
                continue
            try:
                msg = QueuedMessage.from_dict(data)
                if msg.status == MessageStatus.PENDING:
                    self._pending[msg.message_id] = msg
            except (KeyError, ValueError):
                continue

    # ── persistence helpers ───────────────────────────────────────────────────

    async def _load_pending(self) -> None:
        """
        Load pending messages from queue file.

        Reads the JSONL queue file and loads all pending messages
        into the in-memory index. Corrupted or malformed lines are
        skipped without failing the entire load, enabling recovery
        of valid entries from partially-written files (e.g. crash
        mid-write).

        Recovery steps on startup:
        1. Promote orphaned .tmp file if main file is missing
           (crash during atomic write's rename step).
        2. Load valid entries, skipping corrupted lines.
        3. Backup corrupted file before eviction overwrites it.
        4. Rewrite file with only pending entries (eager eviction).

        Side Effects:
            - Populates _pending in-memory dict
            - May rename .tmp → main file
            - May create backup in .data/backups/
        """
        # Recover from crashed atomic write before checking main file
        await self._promote_orphaned_tmp()

        if not self._queue_file.exists():
            log.debug("Queue file does not exist, starting fresh")
            return

        total_lines = 0
        loaded_count = 0
        pending_count = 0
        completed_count = 0
        corrupted_count = 0
        corrupted_line_numbers: List[int] = []
        error_details: List[str] = []

        try:
            # errors='replace' handles non-UTF-8 bytes from crash-induced
            # partial writes by substituting U+FFFD instead of raising
            # UnicodeDecodeError, which would lose all queued messages.
            content = self._queue_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            log.error("Failed to read queue file: %s", exc)
            return

        for line_idx, line in enumerate(content.splitlines(), start=1):
            total_lines += 1
            data = safe_json_parse(line, default=None, log_errors=True, mode=JsonParseMode.LINE)
            if data is None:
                corrupted_count += 1
                corrupted_line_numbers.append(line_idx)
                error_details.append(f"Line {line_idx}: JSON parse failed")
                continue

            try:
                msg = QueuedMessage.from_dict(data)
                loaded_count += 1

                if msg.status == MessageStatus.PENDING:
                    self._pending[msg.message_id] = msg
                    pending_count += 1
                else:
                    completed_count += 1
            except (KeyError, ValueError) as exc:
                corrupted_count += 1
                corrupted_line_numbers.append(line_idx)
                error_details.append(f"Line {line_idx}: {str(exc)[:80]}")
                continue

        is_corrupted = corrupted_count > 0

        # Store structured corruption result for observability
        self._last_corruption_result = QueueCorruptionResult(
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
            # Preserve corrupted data for manual inspection before eviction
            await self._backup_corrupted_file()

        log.info(
            "Loaded queue file: %d entries, %d pending",
            loaded_count,
            pending_count,
        )

        # Eager eviction: rewrite file with only pending entries
        evicted = total_lines - pending_count
        if evicted > 0:
            try:
                await self._persist_pending()
                log.info("Evicted %d non-pending entries from queue file", evicted)
            except Exception as exc:
                log.warning("Failed to evict non-pending entries: %s", exc)

    # ── corruption recovery helpers ────────────────────────────────────────

    async def _promote_orphaned_tmp(self) -> None:
        """Promote orphaned .tmp file to main queue file when main is missing.

        If the process crashed during _persist_pending()'s atomic write
        (specifically between unlink and rename on Windows), the main file
        is deleted and only the .tmp remains. This method promotes the .tmp
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

    async def _backup_corrupted_file(self) -> None:
        """Create a timestamped backup before eviction overwrites corrupted data.

        Follows the same pattern as db_integrity.backup_file_sync: copies the
        corrupted file to .data/backups/ with a timestamped name so that a
        sysadmin can manually inspect or recover data that was skipped during
        line-level parsing.
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

    async def _append_to_queue(self, msg: QueuedMessage) -> None:
        """
        Append a message to the queue file using batched fsync.

        Instead of issuing os.fsync() on every write, lines are accumulated
        in an in-memory buffer.  The buffer is flushed when:
        1. The batch size threshold is reached
        2. The time interval since the last flush has elapsed
        3. close() or _persist_pending() is called

        Args:
            msg: Message to append.

        Side Effects:
            - Buffers line for write coalescing
            - May flush to .data/message_queue.jsonl with fsync
        """
        try:
            line = json_dumps(msg.to_dict(), ensure_ascii=False) + "\n"
            self._write_buffer.append(line)
            await self._maybe_flush_buffer()
        except Exception as exc:
            log.error("Failed to append to queue file: %s", exc)
            raise

    async def _append_completion(self, message_id: str) -> None:
        """
        Append a completion marker to the queue file using batched fsync.

        Instead of rewriting the entire file, we append a completed entry.
        The next _load_pending call will skip completed entries.

        Args:
            message_id: The ID of the completed message.
        """
        try:
            entry = (
                json_dumps(
                    {
                        "message_id": message_id,
                        "status": "completed",
                        "completed_at": time.time(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            self._write_buffer.append(entry)
            await self._maybe_flush_buffer()
        except Exception as exc:
            log.error("Failed to append completion to queue file: %s", exc)
            # Fall back to full persist on error
            self._write_buffer.clear()
            await self._persist_pending()

    async def _maybe_flush_buffer(self) -> None:
        """Flush write buffer if batch-size or time-interval threshold is met."""
        now = time.monotonic()
        size_reached = len(self._write_buffer) >= self._fsync_batch_size
        interval_reached = (
            self._write_buffer
            and (now - self._last_flush_time) >= self._fsync_interval
        )
        if size_reached or interval_reached:
            await self._flush_write_buffer()

    async def _flush_write_buffer(self) -> None:
        """Flush all buffered lines to disk with a single fsync."""
        if not self._write_buffer:
            return
        lines = self._write_buffer
        self._write_buffer = []

        def _write_batch() -> None:
            with self._queue_file.open("a", encoding="utf-8") as f:
                f.writelines(lines)
                f.flush()
                os.fsync(f.fileno())

        try:
            await asyncio.to_thread(_write_batch)
            self._last_flush_time = time.monotonic()
        except Exception as exc:
            log.error("Failed to flush write buffer: %s", exc)
            raise

    async def _flush_loop(self) -> None:
        """Background coroutine that drains the write buffer on a timer.

        Ensures buffered writes are persisted within ``_fsync_interval`` even
        when no new enqueue calls arrive (e.g. during idle periods).  The
        loop sleeps in small increments so it can be cancelled promptly by
        ``close()``.
        """
        try:
            while True:
                await asyncio.sleep(self._fsync_interval)
                if self._write_buffer:
                    async with self._lock:
                        await self._flush_write_buffer()
        except asyncio.CancelledError:
            # Expected during close() — suppress gracefully.
            return

    async def _persist_pending(self) -> None:
        """
        Atomically persist all pending messages to queue file.

        Flushes any buffered writes first, then uses atomic write
        pattern: writes to temp file first, then replaces the target
        file to prevent corruption.

        Side Effects:
            - Flushes write buffer
            - Creates/overwrites .data/message_queue.jsonl
            - Creates temporary .data/message_queue.tmp during write
        """
        # Drain buffer before full rewrite — buffered lines would be
        # overwritten by the atomic write otherwise.
        await self._flush_write_buffer()

        temp_file = self._queue_file.with_suffix(".tmp")

        # Snapshot pending messages to avoid holding lock during I/O
        messages = list(self._pending.values())
        content = "".join(json_dumps(msg.to_dict(), ensure_ascii=False) + "\n" for msg in messages)

        def _atomic_write() -> None:
            from src.utils.async_file import sync_atomic_write

            sync_atomic_write(self._queue_file, content)

        try:
            await asyncio.to_thread(_atomic_write)
            log.debug("Persisted %d pending messages", len(messages))
        except Exception as exc:
            log.error("Failed to persist queue: %s", exc)

            # Clean up temp file if it exists
            tmp = self._queue_file.with_suffix(".tmp")

            def _cleanup() -> None:
                if tmp.exists():
                    tmp.unlink()

            try:
                await asyncio.to_thread(_cleanup)
            except Exception:
                log_noncritical(
                    NonCriticalCategory.QUEUE_OPERATION,
                    "Failed to clean up temp queue file %s",
                    tmp,
                    logger=log,
                )
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Async Context Manager for MessageQueue lifecycle
# ─────────────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager
from typing import AsyncIterator


@asynccontextmanager
async def get_message_queue(
    data_dir: str, stale_timeout: int = MessageQueue.DEFAULT_STALE_TIMEOUT
) -> AsyncIterator[MessageQueue]:
    """
    Async context manager for message queue lifecycle.

    Automatically handles connect() on entry and close() on exit,
    ensuring proper resource cleanup even on exceptions.

    Usage:
        async with get_message_queue(".data") as queue:
            await queue.enqueue(message)
            # ... process message ...
            await queue.complete(message.message_id)

    Args:
        data_dir: Path to directory for queue storage.
        stale_timeout: Seconds after which pending messages are stale.

    Yields:
        MessageQueue: Connected queue instance.
    """
    queue = MessageQueue(data_dir, stale_timeout)
    try:
        await queue.connect()
        yield queue
    finally:
        await queue.close()
