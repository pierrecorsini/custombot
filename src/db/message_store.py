"""
src/db/message_store.py — JSONL message persistence, indexing, and retrieval.

Handles all operations related to reading and writing individual messages
in JSONL format, maintaining the message-ID index for deduplication, and
building message records with checksum calculation and injection scanning.

Extracted from ``db.py`` to isolate the message-persistence responsibility
and make it independently testable.
"""

from __future__ import annotations

import asyncio
import logging
import mmap
import time
import uuid
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, List, Optional

from src.constants import (
    DB_WRITE_MAX_RETRIES,
    DB_WRITE_RETRY_INITIAL_DELAY,
    DEFAULT_DB_TIMEOUT,
    MAX_LRU_CACHE_SIZE,
)
from src.db.db_index import (
    RecoveryResult,
    load_index,
    rebuild_index,
    recover_index,
    save_index,
)
from src.db.db_integrity import (
    calculate_checksum,
    validate_checksum,
)
from src.db.db_utils import (
    MAX_MESSAGE_ID_INDEX,
    MAX_MESSAGE_HISTORY,
    _build_jsonl_header,
    _db_log_extra,
    _sanitize_name,
    _track_db_latency,
    _track_db_write_latency,
    _validate_chat_id,
)
from src.db.file_pool import FileHandlePool
from src.core.errors import NonCriticalCategory, log_noncritical
from src.exceptions import DatabaseError
from src.security.prompt_injection import (
    detect_injection,
    sanitize_user_input,
)
from src.utils import (
    JsonParseMode,
    LRUDict,
    LRULockCache,
    json_dumps,
    safe_json_parse,
)
from src.utils.locking import AsyncLock
from src.utils.path import sanitize_path_component as _sanitize_chat_id_for_path

log = logging.getLogger(__name__)


class MessageStore:
    """Manages JSONL message persistence, indexing, and retrieval.

    Each chat's messages are stored in a separate ``.jsonl`` file under
    the configured messages directory.  A message-ID index provides O(1)
    deduplication lookups.

    Thread-safe via ``asyncio.Lock`` — all operations run inside async
    contexts (``async with lock`` / ``await asyncio.to_thread(...)``).
    """

    # Maximum chunks the reverse-seek will scan before falling back to a
    # simple deque read.  Prevents unbounded looping on corrupted files that
    # lack newline characters.
    _MAX_SEEK_ITERATIONS = 10_000

    def __init__(
        self,
        messages_dir: Path,
        index_file: Path,
        file_pool: FileHandlePool,
        message_locks: LRULockCache,
        message_file_cache: LRUDict,
        check_disk_space_fn,  # Callable[[Path], None]
        guarded_write_fn,  # Callable[[Any, float, str], Awaitable[Any]]
        run_with_timeout_fn,  # Callable[[Any, float, str], Awaitable[Any]]
        atomic_write_fn,  # Callable[[Path, str], None]
    ) -> None:
        self._messages_dir = messages_dir
        self._index_file = index_file
        self._file_pool = file_pool
        self._message_locks = message_locks
        self._message_file_cache = message_file_cache
        self._check_disk_space_before_write = check_disk_space_fn
        self._guarded_write = guarded_write_fn
        self._run_with_timeout = run_with_timeout_fn
        self._atomic_write = atomic_write_fn

        # Deterministic FIFO index: keys are message IDs, values are insertion
        # order. Using dict (ordered in Python 3.7+) for O(1) lookup +
        # deterministic eviction.
        self._message_id_index: dict[str, None] = {}

        # Debounce index persistence: flush on-disk index periodically
        self._index_dirty: bool = False
        self._last_index_save: float = 0.0
        self._index_save_interval: float = 5.0  # seconds

        # Lazy-initialised index lock — AsyncLock defers asyncio.Lock creation
        # until first use, avoiding event-loop binding issues at construction
        # time (see src.utils.locking policy).
        self._index_lock = AsyncLock()

        # Recovery tracking
        self._last_recovery: Optional[RecoveryResult] = None

    # ── index lock ────────────────────────────────────────────────────────────

    def _get_index_lock(self) -> AsyncLock:
        """Return the index lock (AsyncLock — lazy-initialised asyncio.Lock)."""
        return self._index_lock

    # ── message file path ─────────────────────────────────────────────────────

    def message_file(self, chat_id: str) -> Path:
        """Get the message file path for a chat (cached per chat_id)."""
        cached = self._message_file_cache.get(chat_id)
        if cached is not None:
            return cached
        # Sanitize first so special chars (e.g. WhatsApp ':' '@') are replaced
        # before validation rejects them.
        safe_id = _sanitize_chat_id_for_path(chat_id)
        _validate_chat_id(safe_id)
        path = self._messages_dir / f"{safe_id}.jsonl"
        self._message_file_cache[chat_id] = path
        return path

    # ── message index ─────────────────────────────────────────────────────────

    async def ensure_message_index(self) -> None:
        """Load or rebuild message ID index from disk."""
        async with self._get_index_lock():
            loaded = await asyncio.to_thread(load_index, self._index_file)
            if loaded is not None:
                # Convert set to dict for deterministic FIFO eviction order
                self._message_id_index = {mid: None for mid in loaded}
                return

            # Index missing or corrupt — check if file exists for recovery
            if self._index_file.exists():
                ids, recovery = await asyncio.to_thread(
                    recover_index, self._index_file, self._messages_dir,
                )
                self._message_id_index = {mid: None for mid in ids}
                self._last_recovery = recovery
                await asyncio.to_thread(
                    save_index,
                    self._index_file,
                    set(self._message_id_index),
                    self._atomic_write,
                )
                return

            # No file at all — rebuild from scratch
            log.info("message_index.json not found. Rebuilding from message files...")
            ids = await asyncio.to_thread(rebuild_index, self._messages_dir)
            self._message_id_index = {mid: None for mid in ids}
            self._last_recovery = RecoveryResult(
                recovered=True,
                preserved_count=0,
                rebuilt_count=len(ids),
                total_count=len(ids),
            )
            await asyncio.to_thread(
                save_index,
                self._index_file,
                set(self._message_id_index),
                self._atomic_write,
            )

    async def save_message_index(self) -> None:
        """Persist message ID index to disk."""
        await asyncio.to_thread(
            save_index,
            self._index_file,
            set(self._message_id_index),
            self._atomic_write,
        )

    def message_exists(self, message_id: str) -> bool:
        """Check if a message ID exists (O(1) in-memory lookup)."""
        return message_id in self._message_id_index

    def get_recovery_status(self) -> Optional[RecoveryResult]:
        """Get the last recovery result, if any."""
        return self._last_recovery

    def clear_recovery_status(self) -> None:
        """Clear the recovery status after user notification."""
        self._last_recovery = None

    # ── build message record ──────────────────────────────────────────────────

    @staticmethod
    def build_message_record(
        role: str,
        content: Optional[str],
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> tuple[dict, str]:
        """Build a single message dict ready for JSONL persistence.

        Handles ID generation, checksum calculation, and injection scanning
        for user-role messages.

        Returns:
            Tuple of ``(record_dict, message_id)``.
        """
        content = content or ""
        mid = message_id or str(uuid.uuid4())
        timestamp = time.time()
        checksum = calculate_checksum(content, role, timestamp)

        _sanitized = False
        if role == "user" and content:
            result = detect_injection(content)
            if result.detected and result.confidence >= 0.8:
                content = sanitize_user_input(content)
                _sanitized = True
                log.info(
                    "Sanitized injection in user message %s (confidence=%.1f patterns=%s)",
                    mid,
                    result.confidence,
                    result.matched_patterns,
                    extra=_db_log_extra(message_id=mid),
                )

        return {
            "id": mid,
            "role": role,
            "content": content,
            "name": _sanitize_name(name),
            "timestamp": timestamp,
            "_checksum": checksum,
            "_sanitized": _sanitized,
        }, mid

    # ── save operations ───────────────────────────────────────────────────────

    async def save_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        message_id: Optional[str] = None,
        *,
        bump_generation_fn=None,
        trigger_compression_fn=None,
    ) -> str:
        """Append a message to the chat's message file.

        Returns:
            The message ID.
        """
        msg, mid = self.build_message_record(role, content, name, message_id)

        msg_file = self.message_file(chat_id)
        lock = await self._message_locks.get_or_create(chat_id)

        _db_start = time.monotonic()

        async def _write_message():
            async with lock:
                await asyncio.to_thread(
                    self._append_to_file,
                    msg_file,
                    json_dumps(msg, ensure_ascii=False) + "\n",
                )

        await self._guarded_write(
            _write_message(),
            timeout=DEFAULT_DB_TIMEOUT,
            operation="save_message",
        )

        await self._update_index([mid])

        _elapsed_save_msg = time.monotonic() - _db_start
        _track_db_latency(_elapsed_save_msg)
        _track_db_write_latency(_elapsed_save_msg)

        if bump_generation_fn:
            bump_generation_fn(chat_id)

        # Trigger compression check (best-effort, non-critical)
        if trigger_compression_fn:
            try:
                await trigger_compression_fn(chat_id)
            except Exception:
                log_noncritical(
                    NonCriticalCategory.COMPRESSION,
                    "History compression check failed for %s",
                    logger=log,
                    extra=_db_log_extra(chat_id),
                )

        return mid

    async def save_messages_batch(
        self,
        chat_id: str,
        messages: list[dict],
        *,
        bump_generation_fn=None,
        trigger_compression_fn=None,
    ) -> list[str]:
        """Persist multiple messages in a single lock acquisition and file append.

        Each dict in *messages* must have ``role`` and ``content`` keys.
        Optional keys: ``name``, ``message_id``.

        Returns:
            List of message IDs, one per input message.
        """
        from src.utils.retry import calculate_delay_with_jitter

        records: list[dict] = []
        ids: list[str] = []

        for spec in messages:
            msg, mid = self.build_message_record(
                role=spec["role"],
                content=spec["content"],
                name=spec.get("name"),
                message_id=spec.get("message_id"),
            )
            records.append(msg)
            ids.append(mid)

        msg_file = self.message_file(chat_id)
        lock = await self._message_locks.get_or_create(chat_id)

        _db_start = time.monotonic()

        lines = "".join(json_dumps(r, ensure_ascii=False) + "\n" for r in records)

        async def _write_batch():
            async with lock:
                await asyncio.to_thread(self._append_to_file, msg_file, lines)

        delay = DB_WRITE_RETRY_INITIAL_DELAY
        last_os_error: OSError | None = None

        for attempt in range(DB_WRITE_MAX_RETRIES + 1):
            try:
                await self._guarded_write(
                    _write_batch(),
                    timeout=DEFAULT_DB_TIMEOUT,
                    operation="save_messages_batch",
                )
                break  # success — exit retry loop
            except DatabaseError:
                raise  # circuit-breaker / timeout — never retry
            except OSError as exc:
                last_os_error = exc
                if attempt >= DB_WRITE_MAX_RETRIES:
                    log.warning(
                        "DB write retry exhausted after %d attempts for %s: %s",
                        DB_WRITE_MAX_RETRIES + 1,
                        chat_id,
                        exc,
                        extra=_db_log_extra(chat_id),
                    )
                    raise
                actual_delay = calculate_delay_with_jitter(delay)
                log.info(
                    "Transient DB write error for %s, retrying attempt %d/%d after %.2fs: %s",
                    chat_id,
                    attempt + 1,
                    DB_WRITE_MAX_RETRIES,
                    actual_delay,
                    exc,
                    extra=_db_log_extra(chat_id),
                )
                await asyncio.sleep(actual_delay)
                delay *= 2

        await self._update_index(ids)

        _elapsed_batch = time.monotonic() - _db_start
        _track_db_latency(_elapsed_batch)
        _track_db_write_latency(_elapsed_batch)

        if bump_generation_fn:
            bump_generation_fn(chat_id)

        # Trigger compression check (best-effort, non-critical)
        if trigger_compression_fn:
            try:
                await trigger_compression_fn(chat_id)
            except Exception:
                log_noncritical(
                    NonCriticalCategory.COMPRESSION,
                    "History compression check failed for %s",
                    logger=log,
                    extra=_db_log_extra(chat_id),
                )

        return ids

    # ── read operations ───────────────────────────────────────────────────────

    async def get_recent_messages(self, chat_id: str, limit: int = 50) -> List[dict]:
        """Return the last *limit* messages for a chat, oldest first.

        Validates checksums on read and logs any corruption detected.
        Maximum 500 messages can be retrieved (hard cap for memory safety).
        """
        # Enforce hard cap to prevent memory issues
        limit = min(limit, MAX_MESSAGE_HISTORY)

        msg_file = self.message_file(chat_id)

        if not msg_file.exists():
            return []

        lock = await self._message_locks.get_or_create(chat_id)
        _db_start = time.monotonic()

        async def _read_messages():
            async with lock:
                try:
                    lines = await asyncio.to_thread(
                        self._read_file_lines, msg_file, limit,
                    )
                except OSError:
                    return []
            return lines

        try:
            lines = await self._run_with_timeout(
                _read_messages(),
                timeout=DEFAULT_DB_TIMEOUT,
                operation="get_recent_messages",
            )
        except DatabaseError:
            log.warning(
                "Timeout reading messages for chat %s", chat_id,
                extra=_db_log_extra(chat_id),
            )
            return []

        # Lines are already limited to the last N by _read_file_lines
        recent_lines = lines
        messages = []
        corruption_detected = False

        for line_num_offset, line in enumerate(recent_lines):
            msg = safe_json_parse(
                line, default=None, log_errors=False, mode=JsonParseMode.LINE,
            )
            if msg is None:
                if line.strip():  # Only log if line wasn't empty
                    actual_line_num = (
                        len(lines) - len(recent_lines) + line_num_offset + 1
                    )
                    log.warning(
                        "JSON corruption detected in chat %s line %d",
                        chat_id,
                        actual_line_num,
                        extra=_db_log_extra(chat_id),
                    )
                    corruption_detected = True
                continue

            # Validate checksum if present
            is_valid, error = validate_checksum(msg)
            if not is_valid:
                actual_line_num = (
                    len(lines) - len(recent_lines) + line_num_offset + 1
                )
                log.warning(
                    "Checksum validation failed for chat %s line %d: %s",
                    chat_id,
                    actual_line_num,
                    error,
                    extra=_db_log_extra(chat_id),
                )
                corruption_detected = True
                continue

            messages.append(
                {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "name": msg.get("name"),
                    "_sanitized": msg.get("_sanitized", False),
                }
            )

        if corruption_detected:
            log.warning(
                "Corruption detected while reading messages for chat %s. "
                "Some messages may have been skipped. Consider running repair_message_file().",
                chat_id,
                extra=_db_log_extra(chat_id),
            )

        _track_db_latency(time.monotonic() - _db_start)

        # Already in chronological order (oldest first) since we read from file
        return messages

    # ── internal helpers ──────────────────────────────────────────────────────

    def _append_to_file(self, file_path: Path, content: str) -> None:
        """Append content to a file using a pooled file handle.

        Automatically prepends a JSONL schema version header on new/empty files.
        """
        self._check_disk_space_before_write(file_path)
        # Prepend version header on new/empty files
        if not file_path.exists() or file_path.stat().st_size == 0:
            content = _build_jsonl_header() + content
        handle = self._file_pool.get_or_open(file_path)
        handle.write(content)

    def _read_file_lines(
        self, file_path: Path, limit: int = MAX_MESSAGE_HISTORY,
    ) -> List[str]:
        """Read the last N lines from a file without reading the entire file.

        For small files (<64KB), reads normally using deque for O(limit) memory.
        For larger files, uses mmap-based reverse-seek to find line boundaries.
        """
        file_size = file_path.stat().st_size

        # For small files, simple read is faster (avoids seek overhead)
        if file_size < 65_536:
            with file_path.open("r", encoding="utf-8") as f:
                return [line.rstrip("\r") for line in deque(f, maxlen=limit)]

        target_newlines = limit + 1
        newline_count = 0
        iterations = 0
        pos = file_size

        with file_path.open("rb") as f:
            try:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                return [line.rstrip("\r") for line in deque(f, maxlen=limit)]

            with mm:
                while pos > 0 and newline_count < target_newlines:
                    iterations += 1
                    if iterations > self._MAX_SEEK_ITERATIONS:
                        log.warning(
                            "Reverse-seek exceeded %d chunks for %s; "
                            "falling back to deque read (file may be corrupted).",
                            self._MAX_SEEK_ITERATIONS,
                            file_path,
                            extra=_db_log_extra(),
                        )
                        with file_path.open("r", encoding="utf-8") as fb:
                            return [
                                line.rstrip("\r")
                                for line in deque(fb, maxlen=limit)
                            ]

                    region_end = pos
                    region_start = max(0, pos - 8192)
                    for i in range(region_end - 1, region_start - 1, -1):
                        if mm[i] == ord("\n"):
                            newline_count += 1
                            if newline_count >= target_newlines:
                                pos = i + 1
                                break
                    else:
                        pos = region_start

                # Decode directly from mmap — avoids a second open() syscall
                remaining_text = mm[max(pos, 0):].decode("utf-8")

        # Split and take last N lines, preserving order (oldest first)
        all_lines = remaining_text.splitlines()
        selected = all_lines[-limit:] if len(all_lines) > limit else all_lines
        return [line.rstrip("\r") for line in selected]

    async def _update_index(self, message_ids: list[str]) -> None:
        """Add message IDs to the in-memory index with debounce flush."""
        async with self._get_index_lock():
            for mid in message_ids:
                self._message_id_index[mid] = None

            if len(self._message_id_index) > MAX_MESSAGE_ID_INDEX:
                discard_count = MAX_MESSAGE_ID_INDEX // 4
                for _ in range(discard_count):
                    self._message_id_index.popitem(last=False)
                log.warning(
                    "Trimmed %d entries from message ID index (cap=%d)",
                    discard_count,
                    MAX_MESSAGE_ID_INDEX,
                    extra=_db_log_extra(),
                )

            self._index_dirty = True
            now = time.monotonic()
            if (now - self._last_index_save) >= self._index_save_interval:
                await self.save_message_index()
                self._last_index_save = now
                self._index_dirty = False

    @property
    def index_dirty(self) -> bool:
        """Whether the message ID index has unsaved changes."""
        return self._index_dirty

    @index_dirty.setter
    def index_dirty(self, value: bool) -> None:
        self._index_dirty = value
