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

The background flush loop uses a swap-buffers pattern: the write buffer is
detached under the lock (O(1) pointer swap), then flushed to disk *without*
holding the lock so that enqueue/complete calls are never blocked by an
in-progress fsync.  Inline flushes (from enqueue/complete thresholds) flush
directly under the lock since the caller already holds it.

Persistence is delegated to ``QueuePersistence`` (message_queue_persistence.py),
which handles all JSONL file I/O, crash recovery, and integrity validation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator, Dict, List, Optional

from src.constants import MAX_QUEUED_TEXT_LENGTH
from src.core.errors import NonCriticalCategory, log_noncritical
from src.db.db_utils import _validate_chat_id
from src.message_queue_persistence import QueuePersistence
from src.utils import json_dumps
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

    All JSONL file I/O is delegated to ``QueuePersistence``.

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
        self._persistence = QueuePersistence(self._queue_file)

        # In-memory index of pending messages (message_id -> QueuedMessage)
        self._pending: Dict[str, QueuedMessage] = {}

        # Track completed IDs for append-only writes with periodic compaction
        self._completed_since_compact: int = 0
        self._compact_threshold: int = 20  # compact after this many completions

        self._initialized = False

        # Populated after connect() with structured corruption metadata.
        self._last_corruption_result: Optional[QueueCorruptionResult] = None

        # Background task that periodically flushes the write buffer.
        self._flush_task: Optional[asyncio.Task[None]] = None

    async def connect(self) -> None:
        """
        Initialize queue storage and load pending messages.

        Creates the data directory if needed and loads any pending
        messages from disk into memory via the persistence layer.
        """
        self._dir.mkdir(parents=True, exist_ok=True)

        pending, corruption = await asyncio.to_thread(self._persistence.load_pending)
        self._pending = pending
        self._last_corruption_result = corruption

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
        """
        queued = QueuedMessage.from_incoming_message(msg)

        # Truncate excessively long text before persisting to the queue file.
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
        Uses append-only write and periodically compacts the file.

        Args:
            message_id: ID of the message to complete.

        Returns:
            True if message was found and completed, False otherwise.
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

                for msg in stale_messages:
                    log.info(
                        "Recovering stale message %s from chat %s (age: %.1fs)",
                        msg.message_id,
                        msg.chat_id,
                        time.time() - msg.updated_at,
                    )

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

        Safe to call at any time for health checks.
        """
        async with self._lock:
            return await asyncio.to_thread(self._persistence.validate_sync)

    async def repair(self) -> QueueCorruptionResult:
        """
        Detect and repair corruption in the queue file.

        Validates every line, backs up the corrupted file, and rewrites
        it with only valid lines. The in-memory pending index is reloaded
        from the repaired file.
        """
        async with self._lock:
            # Step 1: Detect corruption
            result = await asyncio.to_thread(self._persistence.validate_sync)

            if not result.is_corrupted:
                return result

            # Step 2: Backup before modifying
            await asyncio.to_thread(self._persistence.backup_corrupted_file)

            backup_dir = self._dir / "backups"
            if backup_dir.exists():
                backups = sorted(backup_dir.glob("message_queue_*.jsonl.bak"))
                if backups:
                    result.backup_path = str(backups[-1])

            # Step 3: Rewrite with only valid lines
            await asyncio.to_thread(self._persistence.repair_sync, result)
            result.repaired = True

            # Step 4: Reload pending from repaired file
            self._pending.clear()
            reloaded = await asyncio.to_thread(self._persistence.load_valid_lines_sync)
            self._pending.update(reloaded)

            self._last_corruption_result = result

            log.info(
                "Repaired queue file: removed %d corrupted lines, %d valid lines preserved",
                len(result.corrupted_lines),
                result.valid_lines,
            )
            return result

    # ── persistence delegation ──────────────────────────────────────────────

    async def _append_to_queue(self, msg: QueuedMessage) -> None:
        """Buffer a message line for batched write via persistence layer."""
        try:
            line = json_dumps(msg.to_dict(), ensure_ascii=False) + "\n"
            self._persistence.buffer_line(line)
            await self._maybe_flush_buffer()
        except Exception as exc:
            log.error("Failed to append to queue file: %s", exc)
            raise

    async def _append_completion(self, message_id: str) -> None:
        """Buffer a completion marker for batched write via persistence layer."""
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
            self._persistence.buffer_line(entry)
            await self._maybe_flush_buffer()
        except Exception as exc:
            log.error("Failed to append completion to queue file: %s", exc)
            # Fall back to full persist on error
            self._persistence.clear_buffer()
            await self._persist_pending()

    async def _maybe_flush_buffer(self) -> None:
        """Flush write buffer if a threshold is met."""
        if self._persistence.should_flush():
            await self._flush_write_buffer()

    async def _flush_write_buffer(self) -> None:
        """Flush all buffered lines to disk via persistence layer.

        On I/O failure (e.g. disk full), the persistence layer re-buffers
        the failed lines.  We log a warning and suppress the error so that
        ``enqueue`` / ``complete`` callers don't lose messages — the lines
        will be retried on the next flush cycle.
        """
        if not self._persistence.write_buffer:
            return
        try:
            await asyncio.to_thread(self._persistence.flush_buffer)
        except Exception as exc:
            log.warning(
                "Flush failed, %d lines re-buffered for retry: %s",
                len(self._persistence.write_buffer),
                exc,
            )

    async def _flush_loop(self) -> None:
        """Background coroutine that drains the write buffer on a timer.

        Ensures buffered writes are persisted within the fsync interval even
        when no new enqueue calls arrive (e.g. during idle periods).

        Uses the swap-buffers pattern: atomically detaches the write buffer
        under the lock (fast pointer swap), then flushes the detached buffer
        to disk *without* holding the lock.  This prevents enqueue/complete
        calls from blocking behind an fsync syscall under burst traffic.
        """
        try:
            while True:
                await asyncio.sleep(self._persistence.fsync_interval)
                if self._persistence.write_buffer:
                    # Swap: detach the buffer under lock (O(1) pointer swap)
                    async with self._lock:
                        lines = self._persistence.swap_buffer()
                    # Flush the detached buffer without the lock — enqueue/
                    # complete can proceed against the fresh buffer immediately.
                    if lines:
                        try:
                            await asyncio.to_thread(
                                self._persistence.flush_lines, lines
                            )
                        except Exception as exc:
                            log.warning(
                                "Background flush failed, re-buffering %d lines: %s",
                                len(lines),
                                exc,
                            )
                            async with self._lock:
                                self._persistence.rebuffer_lines(lines)
        except asyncio.CancelledError:
            # Expected during close() — suppress gracefully.
            return

    async def _persist_pending(self) -> None:
        """Atomically persist all pending messages to queue file."""
        try:
            await asyncio.to_thread(
                self._persistence.persist_messages, self._pending.values()
            )
            log.debug("Persisted %d pending messages", len(self._pending))
        except Exception as exc:
            log.error("Failed to persist queue: %s", exc)
            try:
                await asyncio.to_thread(self._persistence.cleanup_temp_file)
            except Exception:
                log_noncritical(
                    NonCriticalCategory.QUEUE_OPERATION,
                    "Failed to clean up temp queue file",
                    logger=log,
                )
            raise


# ─────────────────────────────────────────────────────────────────────────────
# Async Context Manager for MessageQueue lifecycle
# ─────────────────────────────────────────────────────────────────────────────


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
