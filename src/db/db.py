"""
db.py — File-based persistence layer (async, no external dependencies).

Storage structure:
    workspace/
    └── .data/
        ├── chats.json          # Chat metadata index
        └── messages/
            ├── <chat_id_1>.jsonl   # Messages (JSONL = one JSON per line)
            └── <chat_id_2>.jsonl

Lock model: Uses AsyncLock (from src.utils.locking) for all file I/O because
all operations run inside async contexts (async with lock / await
asyncio.to_thread(...)).  This ensures only one coroutine accesses a chat's
file at a time without blocking the event loop.  See src.utils.locking for
the full locking policy.

Metrics: All write and read operations (save_message, get_recent_messages,
_save_chats) are instrumented with latency tracking via PerformanceMetrics.

Architecture: The Database class acts as a thin facade that delegates to
focused modules:
  - ``MessageStore``: JSONL message persistence, indexing, and retrieval.
  - ``CompressionService``: Conversation-history compression for long chats.
  - ``FileHandlePool``: Bounded LRU pool of append-mode file handles.
"""

from __future__ import annotations

import asyncio
import atexit
import logging
import os
import shutil
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from src.constants import (
    DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    DB_WRITE_MAX_RETRIES,
    DB_WRITE_RETRY_BUDGET_SECONDS,
    DB_WRITE_RETRY_INITIAL_DELAY,
    DEFAULT_DB_TIMEOUT,
    MAX_FILE_HANDLES,
    MAX_LRU_CACHE_SIZE,
    MAX_READ_FILE_HANDLES,
)
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.locking import AsyncLock
from src.db.compression import CompressionService
from src.db.db_index import RecoveryResult
from src.db.db_integrity import (
    CorruptionResult,
    backup_file_sync,
    detect_corruption_sync,
    repair_file_sync,
    validate_all_sync,
    validate_checksum,
)
from src.db.db_utils import (
    _db_log_extra,
    _track_db_latency,
    _track_db_write_latency,
)
from src.monitoring.tracing import (
    Span,
    db_guarded_write_span,
    db_retry_attempt_span,
    record_exception_safe,
)
from src.db.file_pool import FileHandlePool, ReadHandlePool
from src.db.generations import GenerationCounter
from src.db.message_store import MessageStore
from src.db.write_journal import write_entry, remove_entry, read_stale_entries, clear_journal
from src.db.migration import apply_jsonl_migrations, batch_ensure_jsonl_schema, ensure_jsonl_schema
from src.exceptions import DatabaseError, ErrorCode
from src.utils import (
    JsonParseMode,
    LRUDict,
    LRULockCache,
    json_dumps,
    safe_json_parse,
)
from src.utils.path import sanitize_path_component as _sanitize_chat_id_for_path
from src.utils.validation import _validate_chat_id

log = logging.getLogger(__name__)

# ── backward-compatible re-exports ────────────────────────────────────────

# Re-export so ``from src.db import CorruptionResult`` keeps working.
# Also re-export the type alias for legacy code that references the
# internal _FileHandlePool name.
from src.db.file_pool import FileHandlePool as _FileHandlePool  # noqa: F401
from src.db.db_utils import _sanitize_name  # noqa: F401
from src.db.db_utils import _JSONL_SCHEMA_VERSION, _JSONL_MIGRATIONS  # noqa: F401

__all__ = [
    "ValidationResult",
    "RecoveryResult",
    "CorruptionResult",
    "Database",
    "get_database",
    "_sanitize_chat_id_for_path",
]


# ── constants (module-level for backward compat) ──────────────────────────

# Maximum messages that can be retrieved in a single query (memory safety)
MAX_MESSAGE_HISTORY = 500


def _append_to_file(path: Path, content: str) -> None:
    """Append *content* to *path*, creating the file if it doesn't exist."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(content)


@dataclass(slots=True, frozen=True)
class ChatMessageParams:
    """Parameter bag for :meth:`Database.upsert_chat_and_save_message`.

    Groups the 6 positional arguments into a single immutable dataclass,
    keeping the call-site readable and satisfying PLR0913.
    """

    chat_id: str
    sender_name: str
    role: str
    content: str
    name: Optional[str] = None
    message_id: Optional[str] = None


@dataclass(slots=True)
class ValidationResult:
    """Result of database connection validation."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


def sqlcipher_connection_factory(db_path: str | Path, *, passphrase: str) -> sqlite3.Connection:
    """Create a SQLCipher-encrypted SQLite connection.

    Attempts to use ``pysqlcipher3`` (bundled SQLCipher bindings).
    Falls back to a plain ``sqlite3`` connection with a warning when
    ``pysqlcipher3`` is not installed — allowing the application to
    start in unencrypted mode during development.

    Args:
        db_path: Path to the database file.
        passphrase: Encryption passphrase.

    Returns:
        A ``sqlite3.Connection`` with PRAGMA key applied.

    Raises:
        ImportError: When ``pysqlcipher3`` is not available and
            ``passphrase`` is non-empty (strict mode).
    """
    try:
        from pysqlcipher3 import dbapi2 as sqlcipher

        conn = sqlcipher.connect(str(db_path))
        conn.execute(f"PRAGMA key = '{passphrase}'")
        conn.execute("PRAGMA cipher_compatibility = 3")
        return conn  # type: ignore[return-value]
    except ImportError:
        if passphrase:
            log.warning(
                "pysqlcipher3 not installed — falling back to unencrypted SQLite. "
                "Install pysqlcipher3 for production encryption.",
            )
        return sqlite3.connect(str(db_path))


@dataclass(slots=True)
class SQLCipherConfig:
    """Configuration for SQLCipher-encrypted database connections.

    When ``passphrase`` is non-empty, the Database will attempt to
    use SQLCipher for transparent encryption-at-rest.  Falls back
    to plain SQLite when ``pysqlcipher3`` is not installed.
    """

    passphrase: str = ""
    cipher_compatibility: int = 3


class Database:
    """
    File-based async database using JSON/JSONL files.

    Thin facade that delegates to focused sub-modules:
      - ``MessageStore`` for message CRUD and indexing
      - ``CompressionService`` for history compression
      - ``FileHandlePool`` for pooled file handles

    Thread-safe via asyncio locks. Human-readable and git-friendly.
    """

    def __init__(self, data_dir: str) -> None:
        """
        Initialize database with data directory path.

        Args:
            data_dir: Path to directory for storing data files.
                      Will be created if it doesn't exist.
        """
        self._dir = Path(data_dir)
        self._messages_dir = self._dir / "messages"
        self._chats_file = self._dir / "chats.json"
        self._chats_changelog_file = self._dir / "chats_changelog.jsonl"
        self._chats_compaction_marker = self._dir / "chats.json.compacted"

        # In-memory caches
        self._chats: Dict[str, Dict[str, Any]] = {}

        # Incremental persistence: track which chat IDs changed since last save.
        # On flush, only dirty entries are appended to the changelog (O(dirty)
        # instead of O(total)).  Periodic compaction merges the changelog back
        # into a full chats.json snapshot.
        self._dirty_chat_ids: set[str] = set()
        self._changelog_entries_since_compact: int = 0
        self._changelog_compact_threshold: int = 200  # compact after this many entries

        # Debounce chat saves: only flush to disk after dirty interval.
        # Coalescing: multiple rapid upserts within the debounce window
        # share a single scheduled flush, reducing redundant disk I/O.
        self._chats_dirty: bool = False
        self._last_chats_save: float = 0.0
        self._chats_save_interval: float = 5.0  # seconds
        self._chats_flush_handle: asyncio.TimerHandle | None = None
        self._chats_flush_in_progress: bool = False
        self._chats_flush_future: asyncio.Task | None = None

        # Locks for thread safety.  AsyncLock defers asyncio.Lock creation
        # until first use — see src.utils.locking policy.
        self._chats_lock = AsyncLock()
        self._message_locks = LRULockCache(max_size=MAX_LRU_CACHE_SIZE)

        # Bounded pool of open file handles for message JSONL appends.
        self._file_pool = FileHandlePool(max_size=MAX_FILE_HANDLES)

        # Bounded pool of read-mode file handles for hot-path message retrieval.
        # Reuses open handles across get_recent_messages() calls, eliminating
        # per-read open/close syscalls on the hot path.
        self._read_pool = ReadHandlePool(max_size=MAX_READ_FILE_HANDLES)

        # Cached path resolution: avoids repeated sanitize + validate + Path
        # construction on every DB operation for the same chat_id.
        self._message_file_cache: LRUDict = LRUDict(max_size=MAX_LRU_CACHE_SIZE)

        self._initialized = False

        # Per-chat generation counter for write-conflict detection.
        self._generation_counter = GenerationCounter()

        # Optional vector memory store for embedding compression summaries.
        self._vector_memory: Any = None

        # Write circuit breaker: fast-fails write operations when the
        # filesystem is degraded.
        self._write_breaker = CircuitBreaker(
            failure_threshold=DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
            cooldown_seconds=DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
        )

        # Cross-operation retry budget: caps cumulative retry sleep across all
        # concurrent _guarded_write calls so that N concurrent writes to a
        # degraded filesystem don't each independently back off and amplify
        # I/O pressure.  Protected by _budget_lock for concurrent access.
        self._retry_budget_spent: float = 0.0
        self._retry_budget_resets: int = 0
        self._budget_lock = AsyncLock()
        # Timestamp (monotonic) when the retry budget was last exhausted.
        # Used to estimate recovery ETA for monitoring dashboards.
        self._retry_budget_exhausted_at: float | None = None

        # ── Sub-service construction ──────────────────────────────────────

        # MessageStore handles JSONL persistence, indexing, and retrieval.
        # It receives callables for cross-cutting concerns (disk checks,
        # guarded writes, timeouts, atomic writes) so it remains stateless
        # with respect to Database orchestration logic.
        self._message_store = MessageStore(
            messages_dir=self._messages_dir,
            index_file=self._dir / "message_index.json",
            file_pool=self._file_pool,
            read_pool=self._read_pool,
            message_locks=self._message_locks,
            message_file_cache=self._message_file_cache,
            check_disk_space_fn=self._check_disk_space_before_write,
            guarded_write_fn=self._guarded_write,
            run_with_timeout_fn=self._run_with_timeout,
            atomic_write_fn=self._atomic_write,
        )

        # CompressionService handles history compression for long-lived chats.
        # It shares the message_id_index dict reference with MessageStore
        # so it can prune IDs of archived messages directly.
        self._compression = CompressionService(
            file_pool=self._file_pool,
            messages_dir=self._messages_dir,
            message_file_fn=self._message_store.message_file,
            get_message_lock_fn=self._get_message_lock,
            get_index_lock_fn=self._message_store._get_index_lock,
            message_id_index=self._message_store._message_id_index,
            mark_index_dirty_fn=lambda: setattr(self._message_store, "_index_dirty", True),
            atomic_write_fn=self._atomic_write,
            vector_memory=None,
        )

    # ── lock accessors ────────────────────────────────────────────────────

    async def _get_message_lock(self, chat_id: str) -> asyncio.Lock:
        """Get or create a lock for a specific chat's messages."""
        return await self._message_locks.get_or_create(chat_id)

    # ── timeout / circuit-breaker helpers ──────────────────────────────────

    async def _run_with_timeout(
        self,
        coro: Any,
        timeout: float,
        operation: str,
    ) -> Any:
        """Run a coroutine with a timeout, raising DatabaseError on timeout."""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            log.error(
                "Database operation '%s' timed out after %ss",
                operation,
                timeout,
                extra=_db_log_extra(),
            )
            raise DatabaseError(
                f"Operation timed out after {timeout}s",
                operation=operation,
                timeout=timeout,
            )

    async def _guarded_write(
        self,
        write_fn: Any,
        timeout: float,
        operation: str,
    ) -> Any:
        """Execute a write with retry, budget, and circuit-breaker protection.

        *write_fn* is a zero-arg async callable (factory) so the write can
        be retried on transient ``OSError`` (disk-full, lock-contention)
        with exponential backoff + jitter.  Only after all retries are
        exhausted is a failure recorded on the circuit breaker.

        A **shared retry budget** caps cumulative ``asyncio.sleep`` time
        across all concurrent ``_guarded_write`` calls.  When the budget
        is exhausted, retries are skipped and the error propagates
        immediately, preventing N concurrent writes from amplifying I/O
        pressure on a degraded filesystem.

        Each retry attempt is traced with OpenTelemetry attributes
        (``attempt``, ``delay_seconds``, ``budget_remaining``) so retry
        storms can be correlated with filesystem degradation in trace
        dashboards.
        """
        async with db_guarded_write_span(
            operation=operation,
            max_retries=DB_WRITE_MAX_RETRIES,
            budget_total=DB_WRITE_RETRY_BUDGET_SECONDS,
        ) as span:
            return await self._guarded_write_inner(
                span, write_fn, timeout, operation,
            )

    async def _guarded_write_inner(
        self,
        span: Span,
        write_fn: Any,
        timeout: float,
        operation: str,
    ) -> Any:
        """Retry loop body, separated for span wrapping."""
        if await self._write_breaker.is_open():
            span.set_attribute("custombot.db.breaker_open", True)
            log.warning(
                "DB write circuit breaker OPEN — %s rejected",
                operation,
                extra=_db_log_extra(),
            )
            raise DatabaseError(
                f"Database write circuit breaker open — {operation} rejected",
                operation=operation,
            )

        from src.utils.retry import calculate_delay_with_jitter

        delay = DB_WRITE_RETRY_INITIAL_DELAY

        for attempt in range(DB_WRITE_MAX_RETRIES + 1):
            with db_retry_attempt_span(attempt=attempt) as retry_span:
                try:
                    result = await self._run_with_timeout(
                        write_fn(),
                        timeout,
                        operation,
                    )
                    retry_span.set_attribute("custombot.db.retry.success", True)
                    span.set_attribute("custombot.db.success", True)
                    await self._write_breaker.record_success()
                    # Reset retry budget on success so transient degradation
                    # doesn't permanently disable retries after recovery.
                    async with self._budget_lock:
                        if self._retry_budget_spent > 0:
                            was_exhausted = self._retry_budget_exhausted_at is not None
                            recovery_duration: float | None = None
                            if was_exhausted:
                                recovery_duration = (
                                    time.monotonic() - self._retry_budget_exhausted_at
                                )
                            log.info(
                                "DB write retry budget reset (%.2fs was spent%s)",
                                self._retry_budget_spent,
                                f", recovered in {recovery_duration:.2f}s"
                                if recovery_duration is not None
                                else "",
                                extra=_db_log_extra(),
                            )
                            self._retry_budget_spent = 0.0
                            self._retry_budget_resets += 1
                            self._retry_budget_exhausted_at = None
                    return result
                except DatabaseError as exc:
                    retry_span.set_attribute(
                        "custombot.db.retry.error_type", "timeout",
                    )
                    record_exception_safe(retry_span, exc)
                    await self._write_breaker.record_failure()
                    raise  # timeout — never retry
                except OSError as exc:
                    record_exception_safe(retry_span, exc)
                    if attempt >= DB_WRITE_MAX_RETRIES:
                        span.set_attribute(
                            "custombot.db.retries_exhausted", True,
                        )
                        log.warning(
                            "DB write retry exhausted after %d attempts"
                            " for %s: %s",
                            DB_WRITE_MAX_RETRIES + 1,
                            operation,
                            exc,
                            extra=_db_log_extra(),
                        )
                        await self._write_breaker.record_failure()
                        raise

                    actual_delay = calculate_delay_with_jitter(delay)

                    # Check shared retry budget before sleeping.
                    async with self._budget_lock:
                        budget_remaining = (
                            DB_WRITE_RETRY_BUDGET_SECONDS
                            - self._retry_budget_spent
                        )
                        if budget_remaining < 0.05:
                            span.set_attribute(
                                "custombot.db.budget_exhausted", True,
                            )
                            retry_span.set_attribute(
                                "custombot.db.retry.budget_exhausted", True,
                            )
                            self._retry_budget_exhausted_at = time.monotonic()
                            log.warning(
                                "DB write retry budget exhausted"
                                " (%.1fs/%.1fs spent)"
                                " — failing %s immediately: %s",
                                self._retry_budget_spent,
                                DB_WRITE_RETRY_BUDGET_SECONDS,
                                operation,
                                exc,
                                extra=_db_log_extra(),
                            )
                            await self._write_breaker.record_failure()
                            raise
                        # Clamp sleep to remaining budget.
                        sleep_time = min(actual_delay, budget_remaining)
                        self._retry_budget_spent += sleep_time

                    retry_span.set_attribute(
                        "custombot.db.retry.delay_seconds", sleep_time,
                    )
                    retry_span.set_attribute(
                        "custombot.db.retry.budget_remaining",
                        budget_remaining - sleep_time,
                    )

                    log.info(
                        "Transient DB write error for %s,"
                        " retrying attempt %d/%d"
                        " after %.2fs (budget remaining: %.2fs): %s",
                        operation,
                        attempt + 1,
                        DB_WRITE_MAX_RETRIES,
                        sleep_time,
                        budget_remaining - sleep_time,
                        exc,
                        extra=_db_log_extra(),
                    )
                    await asyncio.sleep(sleep_time)
                    delay *= 2

    @property
    def write_breaker(self) -> CircuitBreaker:
        """Public accessor for the write circuit breaker (health / metrics)."""
        return self._write_breaker

    @property
    def retry_budget_remaining(self) -> float:
        """Remaining retry budget in seconds (approximate, for monitoring).

        Returns the difference between the configured budget cap and the
        cumulative retry sleep consumed by ``_guarded_write`` so far.
        Clamped to 0 — never negative.
        """
        return max(0.0, DB_WRITE_RETRY_BUDGET_SECONDS - self._retry_budget_spent)

    @property
    def retry_budget_ratio(self) -> float:
        """Remaining retry budget as a ratio (0.0 exhausted to 1.0 full).

        Useful for dashboards and alerting: ``retry_budget_ratio < 0.2``
        signals imminent exhaustion.
        """
        return max(0.0, min(1.0, self.retry_budget_remaining / DB_WRITE_RETRY_BUDGET_SECONDS))

    @property
    def retry_budget_recovery_eta_seconds(self) -> float:
        """Estimated seconds until the retry budget fully recovers.

        Returns 0.0 when the budget is not exhausted.  When exhausted,
        returns an estimate based on time since exhaustion and typical
        recovery patterns (budget resets on the next successful write).
        """
        if self._retry_budget_exhausted_at is None:
            return 0.0
        elapsed = time.monotonic() - self._retry_budget_exhausted_at
        # Heuristic: recovery ETA decays over time — longer waits mean
        # a successful write is more likely as the filesystem recovers.
        # Clamp to a reasonable upper bound (the circuit-breaker cooldown).
        return max(0.0, DB_WRITE_CIRCUIT_COOLDOWN_SECONDS - elapsed)

    @property
    def retry_budget_resets(self) -> int:
        """Total number of retry budget resets after successful recovery."""
        return self._retry_budget_resets

    @property
    def changelog_stats(self) -> dict[str, int]:
        """Changelog persistence stats for health/metrics endpoints.

        Returns:
            Dict with ``entries_since_compact`` and ``dirty_chat_ids`` counts
            so operators can monitor compaction frequency and detect pathological
            write patterns (e.g. a single chat flooding upserts).
        """
        return {
            "entries_since_compact": self._changelog_entries_since_compact,
            "dirty_chat_ids": len(self._dirty_chat_ids),
            "compact_threshold": self._changelog_compact_threshold,
        }

    # ── disk / atomic write helpers ───────────────────────────────────────

    def _check_disk_space_before_write(self, path: Path) -> None:
        """Check disk space before write operations. Raises DiskSpaceError if low."""
        from src.db.db_utils import _check_disk_space_before_write as _impl

        _impl(path)

    def _atomic_write(self, file_path: Path, content: str) -> None:
        """Synchronous helper for atomic file writes."""
        from src.db.db_utils import _atomic_write as _impl

        _impl(file_path, content)

    def _write_marker(self, marker_path: Path, content: str = "1") -> None:
        """Atomically write a marker file for crash-safe recovery.

        Uses ``_atomic_write`` instead of ``Path.write_text`` so a partial
        write mid-crash cannot leave a corrupt/empty marker that would cause
        incorrect recovery behaviour in ``connect()``.
        """
        self._atomic_write(marker_path, content)

    # ── vector memory ─────────────────────────────────────────────────────

    def set_vector_memory(self, vector_memory: Any) -> None:
        """Set the optional vector memory store for embedding compression summaries."""
        self._vector_memory = vector_memory
        self._compression.set_vector_memory(vector_memory)

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _validate_connection_sync(self) -> ValidationResult:
        """Synchronous I/O body of :meth:`validate_connection`.

        Runs filesystem checks (exists, read_text, glob) that would otherwise
        block the event loop.  Called via ``asyncio.to_thread()`` so the async
        caller never stalls on I/O.
        """
        errors: List[str] = []
        warnings: List[str] = []
        details: Dict[str, Any] = {
            "data_dir": str(self._dir),
            "messages_dir": str(self._messages_dir),
            "files_checked": [],
        }

        # Check 1: Data directory exists and is writable
        if not self._dir.exists():
            errors.append(f"Data directory does not exist: {self._dir}")
            details["data_dir_exists"] = False
        else:
            details["data_dir_exists"] = True
            if not os.access(self._dir, os.W_OK):
                errors.append(f"Data directory is not writable: {self._dir}")
                details["data_dir_writable"] = False
            else:
                details["data_dir_writable"] = True

        # Check 2: Messages directory exists and is writable
        if not self._messages_dir.exists():
            warnings.append(
                f"Messages directory does not exist (will be created): {self._messages_dir}"
            )
            details["messages_dir_exists"] = False
        else:
            details["messages_dir_exists"] = True
            if not os.access(self._messages_dir, os.W_OK):
                errors.append(f"Messages directory is not writable: {self._messages_dir}")
                details["messages_dir_writable"] = False
            else:
                details["messages_dir_writable"] = True

        # Check 3: Validate chats.json
        if self._chats_file.exists():
            details["files_checked"].append("chats.json")
            try:
                content = self._chats_file.read_text(encoding="utf-8")
                result = safe_json_parse(content, expected_type=dict, mode=JsonParseMode.STRICT)
                if not result.success:
                    if result.error_type == "type":
                        errors.append("chats.json is not a valid JSON object")
                    else:
                        errors.append(f"chats.json is corrupted: {result.error}")
                    details["chats_json_valid"] = False
                else:
                    details["chats_json_valid"] = True
                    details["chats_count"] = len(result.data)
            except OSError as exc:
                errors.append(f"Failed to read chats.json: {exc}")
                details["chats_json_valid"] = False
        else:
            details["chats_json_valid"] = True
            details["chats_count"] = 0

        # Check 4: Validate message index integrity
        index_file = self._dir / "message_index.json"
        if index_file.exists():
            details["files_checked"].append("message_index.json")
            try:
                content = index_file.read_text(encoding="utf-8")
                result = safe_json_parse(content, expected_type=list, mode=JsonParseMode.STRICT)
                if not result.success:
                    if result.error_type == "type":
                        errors.append("message_index.json is not a valid JSON array")
                    else:
                        warnings.append(
                            f"message_index.json is corrupted (will be rebuilt): {result.error}"
                        )
                    details["message_index_valid"] = False
                else:
                    details["message_index_valid"] = True
                    details["indexed_message_count"] = len(result.data)
            except OSError as exc:
                warnings.append(f"Failed to read message_index.json (will be rebuilt): {exc}")
                details["message_index_valid"] = False
        else:
            details["message_index_valid"] = True
            details["indexed_message_count"] = 0

        # Check 5: Validate message files (sample check with checksum validation)
        corrupted_files: List[str] = []
        checksum_errors: List[str] = []
        if self._messages_dir.exists():
            msg_files = list(self._messages_dir.glob("*.jsonl"))
            details["message_files_count"] = len(msg_files)
            for msg_file in msg_files[:10]:
                try:
                    content = msg_file.read_text(encoding="utf-8")
                    for line_num, line in enumerate(content.splitlines(), 1):
                        if not line.strip():
                            continue
                        msg = safe_json_parse(
                            line, default=None, log_errors=False, mode=JsonParseMode.LINE
                        )
                        if msg is None:
                            corrupted_files.append(f"{msg_file.name}:{line_num}")
                            continue
                        is_valid, error = validate_checksum(msg)
                        if not is_valid:
                            checksum_errors.append(f"{msg_file.name}:{line_num}")
                except OSError as exc:
                    corrupted_files.append(f"{msg_file.name}: {exc}")

            if corrupted_files:
                warnings.append(f"Some message files have invalid JSON: {corrupted_files[:3]}")
                details["corrupted_message_files"] = corrupted_files

            if checksum_errors:
                warnings.append(f"Some message files have checksum errors: {checksum_errors[:3]}")
                details["checksum_errors"] = checksum_errors

            if corrupted_files or checksum_errors:
                log.warning(
                    "Corruption detection: %d JSON errors, %d checksum errors in message files",
                    len(corrupted_files),
                    len(checksum_errors),
                )

        valid = len(errors) == 0
        return ValidationResult(
            valid=valid,
            errors=errors,
            warnings=warnings,
            details=details,
        )

    async def validate_connection(self) -> ValidationResult:
        """Validate database connection and file integrity.

        Delegates synchronous filesystem I/O to a thread via
        ``asyncio.to_thread()`` so the event loop is not blocked during
        startup validation.
        """
        return await asyncio.to_thread(self._validate_connection_sync)

    async def connect(self) -> None:
        """Initialize storage directory and load existing data."""
        # Run validation before proceeding
        validation = await self.validate_connection()
        if validation.valid:
            log.debug("Database validation passed: %s", validation.details)
        else:
            log.error("Database validation failed: %s", validation.errors)
        if validation.warnings:
            log.warning("Database validation warnings: %s", validation.warnings)

        self._dir.mkdir(parents=True, exist_ok=True)
        self._messages_dir.mkdir(parents=True, exist_ok=True)

        # Load or initialize chats
        if self._chats_file.exists():
            self._chats = safe_json_parse(
                self._chats_file.read_text(encoding="utf-8"),
                default={},
                expected_type=dict,
                log_errors=True,
            )
        else:
            self._chats = {}

        # Replay incremental changelog on top of the base snapshot.
        # If a compaction marker exists, the snapshot already reflects all
        # changelog entries — skip replay and clean up stale files.
        if self._chats_compaction_marker.exists():
            log.info("Compaction marker found — skipping stale changelog replay")
            try:
                if self._chats_changelog_file.exists():
                    self._chats_changelog_file.unlink()
            except OSError:
                pass
            try:
                self._chats_compaction_marker.unlink()
            except OSError:
                pass
        elif self._chats_changelog_file.exists():
            self._replay_changelog()

        # Seed instruction files from templates into workspace/instructions/
        workspace_root = self._dir.parent
        instructions_dir = workspace_root / "instructions"
        template_instructions = Path(__file__).parent.parent / "templates" / "instructions"
        # Avoid event-loop overhead when template directory is absent or empty
        if template_instructions.is_dir() and any(template_instructions.iterdir()):
            await asyncio.to_thread(
                self._seed_instruction_templates,
                template_instructions,
                instructions_dir,
            )

        # Load message ID index (delegated to MessageStore)
        await self._message_store.ensure_message_index()

        # Migrate existing JSONL files to current schema version (batched)
        if self._messages_dir.exists():
            jsonl_files = list(self._messages_dir.glob("*.jsonl"))
            if jsonl_files:
                errors = await asyncio.to_thread(
                    batch_ensure_jsonl_schema,
                    jsonl_files,
                    self._file_pool.invalidate,
                )
                for filename, error_msg in errors:
                    log.warning(
                        "Failed to migrate JSONL schema for %s: %s",
                        filename,
                        error_msg,
                    )

        self._initialized = True

        # Register instance for health-check budget polling.
        from src.db._budget_helpers import register_db

        register_db(self)

        # Replay stale write-journal entries from a previous crash.
        await self._replay_write_journal()

        # Register synchronous flush on process exit for debounced writes.
        atexit.register(self._sync_flush_on_exit)

    def _sync_flush_on_exit(self) -> None:
        """Synchronous flush of pending debounced writes (atexit handler).

        Called during process teardown to ensure in-flight debounced writes
        are persisted even when the event loop is no longer running.
        """
        if not self._chats_dirty or not self._dirty_chat_ids:
            return
        try:
            self._save_chats_sync()
            log.info(
                "atexit: flushed %d dirty chat entries",
                len(self._dirty_chat_ids),
            )
        except Exception:
            log.warning("atexit: failed to flush dirty chats", exc_info=True)

    def warm_file_handles(self) -> int:
        """Pre-warm ``FileHandlePool`` for all known chats.

        Call after :meth:`connect` — iterates chat IDs loaded from
        ``chats.json`` and opens their JSONL handles via the pool so the
        first write to each chat avoids an ``open()`` syscall.

        Runs synchronously; call via ``asyncio.to_thread()`` to avoid
        blocking the event loop.

        Returns:
            Number of handles opened (excludes chats whose JSONL file
            does not yet exist on disk).
        """
        count = 0
        for chat_id in self._chats:
            try:
                path = self._message_file(chat_id)
                if path.exists():
                    self._file_pool.get_or_open(path)
                    count += 1
            except Exception:
                log.debug(
                    "FileHandle warmup skipped for chat %s", chat_id, exc_info=True
                )
        if count:
            log.info("FileHandlePool pre-warmed %d handles for known chats", count)
        return count

    def _save_chats_sync(self) -> None:
        """Synchronous fallback for saving chats when asyncio is unavailable.

        Used during shutdown when the event loop executor has already been
        torn down and ``asyncio.to_thread`` raises RuntimeError.  Writes a
        full snapshot for safety (ensures chats.json is complete on disk
        without depending on the changelog being replayed).
        """
        from src.utils.json_utils import json_dumps

        content = json_dumps(self._chats, indent=2, ensure_ascii=False)
        self._atomic_write(self._chats_file, content)
        # Write compaction marker to protect against crash before changelog unlink
        self._write_marker(self._chats_compaction_marker)
        # Remove changelog since full snapshot supersedes it
        if self._chats_changelog_file.exists():
            try:
                self._chats_changelog_file.unlink()
            except OSError:
                pass
        try:
            self._chats_compaction_marker.unlink()
        except OSError:
            pass
        self._dirty_chat_ids.clear()
        self._changelog_entries_since_compact = 0
        self._chats_dirty = False
        log.info("Saved chats via synchronous fallback during shutdown")

    def _replay_changelog(self) -> None:
        """Replay incremental changelog entries on top of the base chats snapshot.

        Called during ``connect()`` to pick up writes that were flushed
        incrementally since the last full compaction.
        """
        import json as _json

        entries_replayed = 0
        try:
            with open(self._chats_changelog_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = _json.loads(line)
                    except (_json.JSONDecodeError, ValueError):
                        log.warning("Skipping malformed changelog line: %.80s", line)
                        continue
                    chat_id = entry.get("id")
                    if not isinstance(chat_id, str):
                        continue
                    # Extract the chat data (everything except 'id')
                    chat_data = {k: v for k, v in entry.items() if k != "id"}
                    if chat_data.get("_deleted"):
                        self._chats.pop(chat_id, None)
                    else:
                        self._chats[chat_id] = chat_data
                    entries_replayed += 1
        except OSError:
            log.warning("Failed to read chats changelog", exc_info=True)
            return

        if entries_replayed:
            log.info("Replayed %d changelog entries on top of chats.json", entries_replayed)

    async def _replay_write_journal(self) -> None:
        """Replay stale write-journal entries from a previous crash.

        On startup, any remaining journal entries indicate debounced writes
        that were in-flight when the process crashed.  Replay them by
        triggering a full chats save for the affected IDs.
        """
        entries = read_stale_entries()
        if not entries:
            return

        log.warning(
            "Found %d stale write-journal entries — replaying crash recovery",
            len(entries),
        )

        dirty_ids: set[str] = set()
        for entry in entries:
            chat_id = entry.get("chat_id", "")
            for cid in chat_id.split(","):
                if cid:
                    dirty_ids.add(cid)

        if dirty_ids:
            async with self._chats_lock:
                self._dirty_chat_ids.update(dirty_ids)
                self._chats_dirty = True
                await self._save_chats()
                self._chats_dirty = False

        clear_journal()
        log.info("Write-journal replay completed (%d chat IDs restored)", len(dirty_ids))

    async def close(self) -> None:
        """Flush any pending writes and close database.

        Each flush is wrapped individually so that a failure in one does
        not prevent the others or the handle-pool cleanup from running.
        Failures are logged as warnings rather than propagated so that
        ``close()`` always releases OS file descriptors.
        """
        # Unregister atexit handler — close() flushes explicitly below.
        atexit.unregister(self._sync_flush_on_exit)

        # Cancel any pending coalesced flush — we'll flush synchronously below.
        self._cancel_flush_future()
        if self._chats_flush_handle is not None:
            self._chats_flush_handle.cancel()
            self._chats_flush_handle = None
        self._chats_flush_in_progress = False

        # Flush any debounced chat writes — best-effort during shutdown.
        # Try async path first, fall back to direct synchronous write when
        # the event loop executor is already shut down.
        if self._chats_dirty:
            try:
                await self._save_chats()
                self._chats_dirty = False
            except Exception:
                log.debug("Async save_chats failed during close, trying sync fallback")
                try:
                    self._save_chats_sync()
                except Exception:
                    log.warning(
                        "Failed to flush dirty chats during close — data may be lost",
                        exc_info=True,
                        extra=_db_log_extra(),
                    )
        # Flush debounced index writes via MessageStore
        if self._message_store.index_dirty:
            try:
                await self._message_store.save_message_index()
                self._message_store.index_dirty = False
            except Exception:
                log.debug("Async save_message_index failed during close, trying sync fallback")
                try:
                    self._message_store.save_message_index_sync()
                    self._message_store.index_dirty = False
                except Exception:
                    log.warning(
                        "Failed to flush message index during close",
                        exc_info=True,
                        extra=_db_log_extra(),
                    )
        # Close all pooled file handles to release OS file descriptors
        self._file_pool.close_all()
        self._read_pool.close_all()
        self._initialized = False

    @staticmethod
    def _seed_instruction_templates(
        template_instructions: Path,
        instructions_dir: Path,
    ) -> None:
        """Copy instruction templates to workspace (runs off the event loop)."""
        if not template_instructions.is_dir():
            return
        instructions_dir.mkdir(parents=True, exist_ok=True)
        for template_file in template_instructions.iterdir():
            if template_file.is_file():
                target = instructions_dir / template_file.name
                if not target.exists():
                    shutil.copy2(template_file, target)
                    log.info("Seeded instruction template: %s", target.name)

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        await self.close()

    # ── chats persistence ─────────────────────────────────────────────────

    async def _save_chats(self) -> None:
        """Persist dirty chat entries via incremental changelog.

        Only chat IDs in ``_dirty_chat_ids`` are appended to the changelog
        file (O(dirty)), avoiding a full O(N) re-serialization of the entire
        ``_chats`` dict on every debounce flush.  When the changelog exceeds
        ``_changelog_compact_threshold`` entries, it is merged back into a
        full ``chats.json`` snapshot (compaction).

        If no chats.json snapshot exists yet (first save), writes a full
        snapshot even when the chats dict is empty.

        Wraps the disk write through ``_guarded_write`` so transient
        ``OSError`` (disk-full, lock-contention) are retried with
        exponential backoff before tripping the write circuit breaker.
        """
        if not self._dirty_chat_ids:
            # No dirty entries — but write initial empty snapshot if no
            # chats.json exists yet so that downstream code can assume
            # the file is present after the first _save_chats() call.
            if not self._chats_file.exists():
                await self._compact_chats()
            self._chats_dirty = False
            return

        _db_start = time.monotonic()

        # Decide whether to compact (full snapshot) or append incrementally.
        should_compact = (
            self._changelog_entries_since_compact >= self._changelog_compact_threshold
        )

        if should_compact:
            await self._compact_chats()
        else:
            await self._append_chats_changelog()

        elapsed = time.monotonic() - _db_start
        _track_db_latency(elapsed)
        _track_db_write_latency(elapsed)

    async def _scheduled_chats_flush(self) -> None:
        """Execute a coalesced debounce flush of dirty chat entries.

        Called via ``loop.call_later`` after ``_chats_save_interval``
        seconds.  Multiple rapid ``upsert_chat`` calls within the
        debounce window share this single flush, avoiding redundant
        disk writes.

        Tracks in-progress state so that ``upsert_chat`` skips
        scheduling a redundant flush while one is already running.
        If new dirty entries accumulate during the flush, a single
        follow-up flush is scheduled after completion.

        Respects the guarded-write pipeline for retry / circuit-breaker
        protection.
        """
        self._chats_flush_handle = None
        self._chats_flush_in_progress = True
        try:
            if not self._chats_dirty or not self._dirty_chat_ids:
                self._chats_dirty = False
                return

            async def _flush() -> None:
                async with self._chats_lock:
                    if not self._dirty_chat_ids:
                        self._chats_dirty = False
                        return
                    entry_id = None
                    try:
                        entry_id = write_entry(
                            ",".join(sorted(self._dirty_chat_ids)),
                            {
                                "chats": {k: self._chats[k] for k in self._dirty_chat_ids if k in self._chats},
                                "dirty": list(self._dirty_chat_ids),
                            },
                        )
                        await self._save_chats()
                        self._last_chats_save = time.time()
                        self._chats_dirty = False
                    finally:
                        # Remove journal entry on success; keep on failure
                        # so it can be replayed on next startup.
                        if entry_id and not self._chats_dirty:
                            remove_entry(entry_id)

            await self._guarded_write(
                _flush,
                timeout=DEFAULT_DB_TIMEOUT,
                operation="scheduled_chats_flush",
            )
        finally:
            self._chats_flush_in_progress = False

        # If new dirty entries accumulated during the flush, schedule
        # a single follow-up flush to persist them.
        if self._chats_dirty and self._chats_flush_handle is None:
            loop = asyncio.get_running_loop()
            self._chats_flush_handle = loop.call_later(
                self._chats_save_interval,
                self._start_tracked_flush,
            )

    async def _compact_chats(self) -> None:
        """Write a full chats.json snapshot and clear the changelog."""
        content = json_dumps(self._chats, indent=2, ensure_ascii=False)

        async def _write_full():
            try:
                await asyncio.to_thread(self._atomic_write, self._chats_file, content)
            except RuntimeError as exc:
                if "cannot schedule new futures after shutdown" not in str(exc):
                    raise
                log.warning(
                    "Default executor unavailable while saving chats; using synchronous fallback"
                )
                self._atomic_write(self._chats_file, content)
            # Write compaction marker — if we crash before unlinking the
            # changelog, the marker tells connect() to skip stale replay.
            self._write_marker(self._chats_compaction_marker)
            if self._chats_changelog_file.exists():
                self._chats_changelog_file.unlink()
            # Marker no longer needed once changelog is removed
            try:
                self._chats_compaction_marker.unlink()
            except OSError:
                pass

        await self._guarded_write(
            _write_full,
            timeout=DEFAULT_DB_TIMEOUT,
            operation="save_chats_compact",
        )

        self._dirty_chat_ids.clear()
        self._changelog_entries_since_compact = 0
        log.debug("Compacted chats to full snapshot (%d chats)", len(self._chats))

    async def _append_chats_changelog(self) -> None:
        """Append only dirty chat entries to the incremental changelog."""
        lines: list[str] = []
        for chat_id in self._dirty_chat_ids:
            chat_data = self._chats.get(chat_id)
            if chat_data is not None:
                lines.append(json_dumps({"id": chat_id, **chat_data}))
        if not lines:
            self._dirty_chat_ids.clear()
            return

        content = "".join(line + "\n" for line in lines)
        changelog_path = self._chats_changelog_file

        async def _append():
            try:
                await asyncio.to_thread(
                    _append_to_file, changelog_path, content
                )
            except RuntimeError as exc:
                if "cannot schedule new futures after shutdown" not in str(exc):
                    raise
                log.warning(
                    "Default executor unavailable while appending changelog; using sync fallback"
                )
                _append_to_file(changelog_path, content)

        await self._guarded_write(
            _append,
            timeout=DEFAULT_DB_TIMEOUT,
            operation="save_chats_incremental",
        )

        self._changelog_entries_since_compact += len(lines)
        self._dirty_chat_ids.clear()

    def _start_tracked_flush(self) -> None:
        """Start a tracked flush coroutine, storing the future reference.

        Replaces bare ``asyncio.ensure_future()`` so that exceptions in
        the flush coroutine are logged via ``_on_flush_done`` instead of
        producing "Task exception was never retrieved" warnings.
        """
        self._chats_flush_future = asyncio.ensure_future(
            self._scheduled_chats_flush()
        )
        self._chats_flush_future.add_done_callback(self._on_flush_done)

    @staticmethod
    def _on_flush_done(fut: asyncio.Task) -> None:
        """Done-callback for tracked flush futures.

        Logs exceptions using the non-critical error system so they're
        observable without crashing the event loop.  Silently ignores
        ``CancelledError`` (expected during shutdown).
        """
        if fut.cancelled():
            return
        exc = fut.exception()
        if exc is not None:
            from src.core.errors import NonCriticalCategory, log_noncritical

            log_noncritical(
                NonCriticalCategory.DB_OPERATION,
                "Scheduled chats flush failed",
                logger=log,
                extra={"operation": "scheduled_chats_flush"},
            )

    def _cancel_flush_future(self) -> None:
        """Cancel the tracked flush future if one is running."""
        if self._chats_flush_future is not None:
            self._chats_flush_future.cancel()
            self._chats_flush_future = None

    def _schedule_chats_flush(self) -> None:
        """Schedule a coalesced debounce flush if one isn't already pending.

        Must be called while holding ``_chats_lock``.  If a flush is already
        scheduled or in progress, skip — all accumulated dirty entries will
        be written together when the timer fires (or in a follow-up flush
        after the current one completes).
        """
        if (
            self._chats_flush_handle is None
            and not self._chats_flush_in_progress
        ):
            loop = asyncio.get_running_loop()
            self._chats_flush_handle = loop.call_later(
                self._chats_save_interval,
                self._start_tracked_flush,
            )

    async def upsert_chat(self, chat_id: str, name: Optional[str] = None) -> None:
        """Create or update a chat's metadata (debounced save)."""
        now = time.time()

        async def _upsert():
            async with self._chats_lock:
                if chat_id in self._chats:
                    self._chats[chat_id]["last_active"] = now
                    if name:
                        self._chats[chat_id]["name"] = name
                else:
                    self._chats[chat_id] = {
                        "name": name,
                        "created_at": now,
                        "last_active": now,
                        "metadata": {},
                    }

                self._chats_dirty = True
                self._dirty_chat_ids.add(chat_id)
                self._schedule_chats_flush()

        await self._guarded_write(
            _upsert,
            timeout=DEFAULT_DB_TIMEOUT,
            operation="upsert_chat",
        )

    async def upsert_chat_and_save_message(
        self,
        params: ChatMessageParams,
    ) -> str:
        """Upsert chat metadata and persist a message in one logical operation.

        Performs the lightweight in-memory chat update inline (no
        ``_guarded_write``) and delegates only the message write through the
        guarded pipeline, reducing circuit-breaker checks and retry overhead
        from two calls to one.
        """
        now = time.time()
        async with self._chats_lock:
            if params.chat_id in self._chats:
                self._chats[params.chat_id]["last_active"] = now
                if params.sender_name:
                    self._chats[params.chat_id]["name"] = params.sender_name
            else:
                self._chats[params.chat_id] = {
                    "name": params.sender_name,
                    "created_at": now,
                    "last_active": now,
                    "metadata": {},
                }
            self._chats_dirty = True
            self._dirty_chat_ids.add(params.chat_id)
            self._schedule_chats_flush()

        return await self.save_message(
            chat_id=params.chat_id,
            role=params.role,
            content=params.content,
            name=params.name,
            message_id=params.message_id,
        )

    async def flush_chats(self) -> None:
        """Force-flush dirty chat metadata to disk.

        Always writes a full ``chats.json`` snapshot (compaction) so that
        callers can rely on ``chats.json`` being authoritative after this
        method returns.  The incremental changelog, if any, is removed.
        Cancels any pending coalesced debounce flush to avoid redundancy.
        """
        # Cancel any pending scheduled flush — we're doing it now.
        self._cancel_flush_future()
        if self._chats_flush_handle is not None:
            self._chats_flush_handle.cancel()
            self._chats_flush_handle = None
        self._chats_flush_in_progress = False

        async with self._chats_lock:
            if self._chats_dirty or self._chats_changelog_file.exists():
                await self._compact_chats()
                self._last_chats_save = time.time()
                self._chats_dirty = False

    async def list_chats(self) -> List[dict]:
        """List all chats sorted by last_active (most recent first)."""
        async with self._chats_lock:
            chats = [
                {
                    "chat_id": chat_id,
                    **chat_data,
                }
                for chat_id, chat_data in self._chats.items()
            ]

        chats.sort(key=lambda x: x.get("last_active", 0), reverse=True)
        return chats

    # ── message delegation ─────────────────────────────────────────────────

    def _message_file(self, chat_id: str) -> Path:
        """Get the message file path for a chat (delegates to MessageStore)."""
        return self._message_store.message_file(chat_id)

    async def message_exists(self, message_id: str) -> bool:
        """Check if a message ID exists (O(1) in-memory lookup)."""
        return self._message_store.message_exists(message_id)

    async def batch_message_exists(self, message_ids: list[str]) -> dict[str, bool]:
        """Batch-check which message IDs exist (single-pass in-memory lookup)."""
        return self._message_store.batch_message_exists(message_ids)

    def get_recovery_status(self) -> Optional[RecoveryResult]:
        """Get the last recovery result, if any."""
        return self._message_store.get_recovery_status()

    def clear_recovery_status(self) -> None:
        """Clear the recovery status after user notification."""
        self._message_store.clear_recovery_status()

    @staticmethod
    def _build_message_record(
        role: str,
        content: Optional[str],
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> tuple[dict, str]:
        """Build a single message dict ready for JSONL persistence.

        Delegates to ``MessageStore.build_message_record``.
        """
        return MessageStore.build_message_record(role, content, name, message_id)

    async def save_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """Append a message to the chat's message file.

        Delegates to ``MessageStore.save_message`` with generation-bump and
        compression-trigger hooks.
        """
        return await self._message_store.save_message(
            chat_id,
            role,
            content,
            name,
            message_id,
            bump_generation_fn=self._bump_generation,
            trigger_compression_fn=self._compression.compress_chat_history,
        )

    async def save_messages_batch(
        self,
        chat_id: str,
        messages: list[dict],
    ) -> list[str]:
        """Persist multiple messages in a single lock acquisition.

        Delegates to ``MessageStore.save_messages_batch`` with generation-bump
        and compression-trigger hooks.
        """
        return await self._message_store.save_messages_batch(
            chat_id,
            messages,
            bump_generation_fn=self._bump_generation,
            trigger_compression_fn=self._compression.compress_chat_history,
        )

    async def get_recent_messages(self, chat_id: str, limit: int = 50) -> List[dict]:
        """Return the last *limit* messages for a chat, oldest first.

        Clamps *limit* to :data:`MAX_MESSAGE_HISTORY` to prevent unbounded
        memory usage from accidental large queries.

        Delegates to ``MessageStore.get_recent_messages``.
        """
        safe_limit = max(1, min(limit, MAX_MESSAGE_HISTORY))
        return await self._message_store.get_recent_messages(chat_id, safe_limit)

    # ── generation counter for write-conflict detection ────────────────────

    def get_generation(self, chat_id: str) -> int:
        """Return the current generation counter for a chat."""
        return self._generation_counter.get(chat_id)

    def check_generation(self, chat_id: str, expected: int) -> bool:
        """Return True if the chat's generation still matches *expected*."""
        return self._generation_counter.check(chat_id, expected)

    def _bump_generation(self, chat_id: str) -> None:
        """Increment the generation counter after a successful write."""
        self._generation_counter.bump(chat_id)

    # ── JSONL schema migration (delegates to migration module) ────────────

    def _ensure_jsonl_schema(self, file_path: Path) -> None:
        """Ensure a JSONL file has the current schema header.

        Delegates to :func:`src.db.migration.ensure_jsonl_schema`.
        """
        ensure_jsonl_schema(file_path, invalidate_fn=self._file_pool.invalidate)

    def _apply_jsonl_migrations(self, file_path: Path, current_version: int) -> None:
        """Apply incremental JSONL schema migrations.

        Delegates to :func:`src.db.migration.apply_jsonl_migrations`.
        """
        apply_jsonl_migrations(
            file_path,
            current_version,
            invalidate_fn=self._file_pool.invalidate,
        )

    # ── compression delegation ─────────────────────────────────────────────

    def _compressed_summary_file(self, chat_id: str) -> Path:
        """Get the compressed summary file path for a chat."""
        return self._compression.compressed_summary_file(chat_id)

    async def get_compressed_summary(self, chat_id: str) -> str | None:
        """Return the compressed history summary text for a chat, if any."""
        return await self._compression.get_compressed_summary(chat_id)

    async def compress_chat_history(self, chat_id: str) -> bool:
        """Compress a chat's history when the JSONL exceeds the line threshold.

        Delegates to ``CompressionService.compress_chat_history``.
        """
        return await self._compression.compress_chat_history(chat_id)

    # ── corruption detection (delegates to db_integrity) ────────────────────

    async def detect_corruption(self, chat_id: str) -> CorruptionResult:
        """Detect corruption in a chat's message file."""
        msg_file = self._message_file(chat_id)
        return await asyncio.to_thread(detect_corruption_sync, msg_file)

    async def backup_corrupted_file(self, chat_id: str) -> Optional[str]:
        """Create a backup of a potentially corrupted message file."""
        msg_file = self._message_file(chat_id)
        return await asyncio.to_thread(backup_file_sync, msg_file, self._dir)

    async def repair_message_file(self, chat_id: str, backup: bool = True) -> CorruptionResult:
        """Detect and repair corruption, optionally backing up first."""
        result = await self.detect_corruption(chat_id)
        if not result.is_corrupted:
            return result

        if backup:
            result.backup_path = await self.backup_corrupted_file(chat_id)

        msg_file = self._message_file(chat_id)
        lock = await self._get_message_lock(chat_id)
        async with lock:
            self._file_pool.invalidate(msg_file)
            result.repaired = await asyncio.to_thread(
                repair_file_sync, msg_file, result, self._atomic_write
            )
        return result

    async def validate_all_message_files(self, repair: bool = False) -> Dict[str, CorruptionResult]:
        """Validate (and optionally repair) all message files."""
        if repair:
            results: Dict[str, CorruptionResult] = {}
            if not self._messages_dir.exists():
                return results
            for msg_file in self._messages_dir.glob("*.jsonl"):
                chat_id = msg_file.stem
                try:
                    _validate_chat_id(chat_id)
                except ValueError:
                    log.warning(
                        "Skipping message file with invalid chat_id stem: %s",
                        msg_file.name,
                    )
                    continue
                results[chat_id] = await self.repair_message_file(chat_id, backup=True)
            return results

        return await asyncio.to_thread(validate_all_sync, self._messages_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Async Context Manager for Database lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def get_database(data_dir: str) -> AsyncIterator[Database]:
    """Async context manager for database lifecycle management."""
    db = Database(data_dir)
    try:
        await db.connect()
        yield db
    finally:
        await db.close()
