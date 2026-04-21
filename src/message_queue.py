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

Lock model: Uses asyncio.Lock for file I/O (same pattern as db.py).
The lock is lazily initialised on first use (see ``_get_lock()``) to
avoid binding to an event loop at construction time. All queue operations
are async and wrapped in await asyncio.to_thread() for the actual disk
writes. asyncio.Lock ensures coroutine-safe access without blocking the
event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from src.constants import MAX_QUEUED_TEXT_LENGTH
from src.utils import JsonParseMode, json_dumps, safe_json_parse

if TYPE_CHECKING:
    from src.channels.base import IncomingMessage

log = logging.getLogger(__name__)


class MessageStatus(str, Enum):
    """Status of a queued message."""

    PENDING = "pending"
    COMPLETED = "completed"


@dataclass
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
        """Create from dictionary (JSON deserialization)."""
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


class MessageQueue:
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
        self._dir = Path(data_dir)
        self._queue_file = self._dir / "message_queue.jsonl"
        self._stale_timeout = stale_timeout

        # In-memory index of pending messages (message_id -> QueuedMessage)
        self._pending: Dict[str, QueuedMessage] = {}

        # Track completed IDs for append-only writes with periodic compaction
        self._completed_since_compact: int = 0
        self._compact_threshold: int = 20  # compact after this many completions

        # Lock for thread-safe operations (lazy-initialised to avoid binding
        # to an event loop that may not be running at construction time,
        # e.g. during test fixture setup on Windows with ProactorEventLoop).
        self._lock: asyncio.Lock | None = None

        self._initialized = False

    def _get_lock(self) -> asyncio.Lock:
        """Return the asyncio lock, creating it on first use.

        Mirrors the pattern in ``channels.base._get_safe_mode_lock()``.
        ``asyncio.Lock()`` must be created inside a running event loop on
        Python 3.10+, so we defer creation until the first actual use.
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

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
        log.info(
            "Message queue initialized with %d pending messages",
            len(self._pending),
        )

    async def close(self) -> None:
        """
        Flush pending messages and close queue.

        Ensures all pending messages are persisted to disk.

        Side Effects:
            - Persists pending messages to disk
            - Sets _initialized flag to False
        """
        async with self._get_lock():
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

        async with self._get_lock():
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
        async with self._get_lock():
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

        async with self._get_lock():
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
        async with self._get_lock():
            return len(self._pending)

    async def get_pending_for_chat(self, chat_id: str) -> List[QueuedMessage]:
        """
        Get all pending messages for a specific chat.

        Args:
            chat_id: Chat/group ID to filter by.

        Returns:
            List of pending messages for the chat.
        """
        async with self._get_lock():
            return [msg for msg in self._pending.values() if msg.chat_id == chat_id]

    # ── persistence helpers ───────────────────────────────────────────────────

    async def _load_pending(self) -> None:
        """
        Load pending messages from queue file.

        Reads the JSONL queue file and loads all pending messages
        into the in-memory index. Completed messages are skipped.

        Side Effects:
            - Populates _pending in-memory dict
        """
        if not self._queue_file.exists():
            log.debug("Queue file does not exist, starting fresh")
            return

        total_lines = 0
        loaded_count = 0
        pending_count = 0

        try:
            content = self._queue_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                total_lines += 1
                data = safe_json_parse(line, default=None, log_errors=True, mode=JsonParseMode.LINE)
                if data is None:
                    continue

                try:
                    msg = QueuedMessage.from_dict(data)
                    loaded_count += 1

                    # Only load pending messages; completed ones are skipped
                    if msg.status == MessageStatus.PENDING:
                        self._pending[msg.message_id] = msg
                        pending_count += 1
                except KeyError as e:
                    log.warning(
                        "Skipping invalid queue entry (missing key): %s",
                        str(e)[:100],
                    )
                    continue

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

        except Exception as e:
            log.error("Failed to load queue file: %s", e)
            # Continue with empty queue rather than crash
            self._pending = {}

    async def _append_to_queue(self, msg: QueuedMessage) -> None:
        """
        Append a message to the queue file.

        Uses atomic append for thread safety.

        Args:
            msg: Message to append.

        Side Effects:
            - Appends to .data/message_queue.jsonl
        """
        try:
            line = json_dumps(msg.to_dict(), ensure_ascii=False) + "\n"

            def _write() -> None:
                with self._queue_file.open("a", encoding="utf-8") as f:
                    f.write(line)

            await asyncio.to_thread(_write)
        except Exception as e:
            log.error("Failed to append to queue file: %s", e)
            raise

    async def _append_completion(self, message_id: str) -> None:
        """
        Append a completion marker to the queue file (append-only optimization).

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

            def _write() -> None:
                with self._queue_file.open("a", encoding="utf-8") as f:
                    f.write(entry)

            await asyncio.to_thread(_write)
        except Exception as e:
            log.error("Failed to append completion to queue file: %s", e)
            # Fall back to full persist on error
            await self._persist_pending()

    async def _persist_pending(self) -> None:
        """
        Atomically persist all pending messages to queue file.

        Uses atomic write pattern: writes to temp file first, then
        replaces the target file to prevent corruption.

        Side Effects:
            - Creates/overwrites .data/message_queue.jsonl
            - Creates temporary .data/message_queue.tmp during write
        """
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
        except Exception as e:
            log.error("Failed to persist queue: %s", e)

            # Clean up temp file if it exists
            tmp = self._queue_file.with_suffix(".tmp")

            def _cleanup() -> None:
                if tmp.exists():
                    tmp.unlink()

            try:
                await asyncio.to_thread(_cleanup)
            except Exception:
                pass
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
