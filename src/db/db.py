"""
db.py — File-based persistence layer (async, no external dependencies).

Storage structure:
    workspace/
    └── .data/
        ├── chats.json          # Chat metadata index
        └── messages/
            ├── <chat_id_1>.jsonl   # Messages (JSONL = one JSON per line)
            └── <chat_id_2>.jsonl

Lock model: Uses asyncio.Lock for all file I/O because all operations run
inside async contexts (async with lock / await asyncio.to_thread(...)).
This ensures only one coroutine accesses a chat's file at a time without
blocking the event loop. Never use threading.Lock here — it would block
the event loop while waiting for the lock.

Metrics: All write and read operations (save_message, get_recent_messages,
_save_chats) are instrumented with latency tracking via PerformanceMetrics.
"""

from __future__ import annotations

import asyncio
import logging
import mmap
import os
import re
import shutil
import threading
import time
import uuid
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, IO, List, Optional

from src.constants import (
    COMPRESSION_KEEP_RECENT,
    COMPRESSION_LINE_THRESHOLD,
    DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    DEFAULT_DB_TIMEOUT,
    MAX_CHAT_GENERATIONS,
    MAX_FILE_HANDLES,
    MAX_LRU_CACHE_SIZE,
)
from src.utils.circuit_breaker import CircuitBreaker
from src.db.db_index import (
    RecoveryResult,
    load_index,
    rebuild_index,
    recover_index,
    save_index,
)
from src.db.db_integrity import (
    CorruptionResult,
    MessageLine,
    backup_file_sync,
    calculate_checksum,
    detect_corruption_sync,
    repair_file_sync,
    validate_all_sync,
    validate_checksum,
)
from src.exceptions import DatabaseError, DiskSpaceError
from src.logging import get_correlation_id
from src.utils import (
    DEFAULT_MIN_DISK_SPACE,
    JsonParseMode,
    LRUDict,
    LRULockCache,
    check_disk_space,
    json_dumps,
    json_loads,
    safe_json_parse,
)

log = logging.getLogger(__name__)


def _track_db_latency(elapsed_seconds: float) -> None:
    """Record a database operation latency in the global metrics collector."""
    try:
        from src.monitoring.performance import get_metrics_collector

        get_metrics_collector().track_db_latency(elapsed_seconds)
    except Exception:
        pass  # Metrics tracking must never crash DB operations


def _db_log_extra(chat_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    """Build structured extra dict for DB log statements with correlation ID.

    Ensures every DB log statement carries the current correlation ID from
    the request context, enabling end-to-end request tracing across the
    message pipeline → database layer.
    """
    extra: dict[str, Any] = {"correlation_id": get_correlation_id()}
    if chat_id is not None:
        extra["chat_id"] = chat_id
    extra.update(kwargs)
    return extra


# Maximum messages that can be retrieved in a single query (memory safety)
MAX_MESSAGE_HISTORY = 500

# Maximum entries in the message ID index (prevents unbounded memory growth)
MAX_MESSAGE_ID_INDEX = 100_000

# JSONL schema version for forward-compatible message format changes.
# Each message file starts with a header line: {"_version": N, "type": "header"}
# Migrations in _JSONL_MIGRATIONS backfill missing fields when upgrading.
_JSONL_SCHEMA_VERSION = 1
_JSONL_MIGRATIONS: list[tuple[int, list[Any]]] = []  # (target_version, [migration_fns])

# Pattern for valid chat_id (safe for file paths)
_CHAT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.\@]+$")

# Maximum length for the 'name' field persisted to JSONL (prevents unbounded storage).
_MAX_NAME_LENGTH = 200

# Control characters (C0 + C1 ranges, except common whitespace) stripped from names.
_CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")

from src.utils.path import sanitize_path_component as _sanitize_chat_id_for_path


def _sanitize_name(name: Optional[str]) -> Optional[str]:
    """Sanitize a sender/tool name before persisting to JSONL.

    Strips control characters and truncates to ``_MAX_NAME_LENGTH``.
    Returns ``None`` if the name is empty after sanitization.
    """
    if not name:
        return None
    cleaned = _CONTROL_CHAR_PATTERN.sub("", name)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_NAME_LENGTH:
        cleaned = cleaned[:_MAX_NAME_LENGTH]
    return cleaned


def _validate_chat_id(chat_id: str) -> None:
    """
    Validate chat_id format for safe file path usage.

    Args:
        chat_id: The chat ID to validate.

    Raises:
        ValueError: If chat_id contains unsafe characters.
    """
    if not chat_id:
        raise ValueError("chat_id cannot be empty")
    if not _CHAT_ID_PATTERN.match(chat_id):
        raise ValueError(
            f"Invalid chat_id format: {chat_id!r}. "
            "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
        )


def _build_jsonl_header() -> str:
    """Return the JSONL schema version header line (with trailing newline)."""
    return json_dumps({"_version": _JSONL_SCHEMA_VERSION, "type": "header"}) + "\n"


# Re-export so ``from src.db import CorruptionResult`` keeps working
__all__ = [
    "ValidationResult",
    "RecoveryResult",
    "CorruptionResult",
    "MessageLine",
    "Database",
    "get_database",
    "_validate_chat_id",
    "_sanitize_chat_id_for_path",
]


@dataclass
class ValidationResult:
    """Result of database connection validation."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


class _FileHandlePool:
    """Bounded LRU pool of append-mode file handles for message JSONL files.

    Prevents OS file-descriptor exhaustion under extreme concurrency by reusing
    open file handles across writes instead of open/close per operation.

    Thread-safe via ``threading.Lock`` because file I/O runs inside
    ``asyncio.to_thread()`` workers (no event loop available in those threads).
    Per-chat asyncio locks already guarantee that only one thread accesses a
    given handle at a time, so the pool itself only needs to protect the
    OrderedDict metadata.

    Handles are opened in line-buffered append mode (``buffering=1``) so every
    newline-terminated JSONL record is flushed to the OS immediately, matching
    the durability guarantee of the previous open/write/close pattern.
    """

    __slots__ = ("_handles", "_lock", "_max_size")

    def __init__(self, max_size: int = MAX_FILE_HANDLES) -> None:
        self._max_size = max_size
        self._handles: OrderedDict[str, IO[str]] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_open(self, path: Path) -> IO[str]:
        """Return an open append-mode handle for *path*, creating one if needed.

        LRU-evicts the least-recently-used handle when the pool exceeds
        *max_size*, closing the evicted handle.
        """
        key = str(path)
        with self._lock:
            if key in self._handles:
                handle = self._handles[key]
                if not handle.closed:
                    self._handles.move_to_end(key)
                    return handle
                # Stale handle — remove and reopen
                del self._handles[key]

            handle = path.open("a", encoding="utf-8", buffering=1)
            self._handles[key] = handle

            # Evict LRU entries over capacity
            while len(self._handles) > self._max_size:
                _, evicted = self._handles.popitem(last=False)
                self._close_handle(evicted)

            return handle

    def invalidate(self, path: Path) -> None:
        """Close and remove the handle for *path* (e.g. after file repair)."""
        key = str(path)
        with self._lock:
            handle = self._handles.pop(key, None)
            if handle is not None:
                self._close_handle(handle)

    def close_all(self) -> None:
        """Close every pooled handle.  Called during ``Database.close()``."""
        with self._lock:
            handles = list(self._handles.values())
            self._handles.clear()
        for handle in handles:
            self._close_handle(handle)

    @staticmethod
    def _close_handle(handle: IO[str]) -> None:
        try:
            if not handle.closed:
                handle.close()
        except Exception:
            pass  # Best-effort close during shutdown


class Database:
    """
    File-based async database using JSON/JSONL files.

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

        # In-memory caches
        self._chats: Dict[str, Dict[str, Any]] = {}
        # Deterministic FIFO index: keys are message IDs, values are insertion order
        # Using dict (ordered in Python 3.7+) for O(1) lookup + deterministic eviction
        self._message_id_index: Dict[str, None] = {}

        # Debounce chat saves: only flush to disk after dirty interval
        self._chats_dirty: bool = False
        self._last_chats_save: float = 0.0
        self._chats_save_interval: float = 5.0  # seconds

        # Debounce index persistence: flush on-disk index periodically
        # so the on-disk index is never more than _index_save_interval
        # seconds behind the in-memory index.
        self._index_dirty: bool = False
        self._last_index_save: float = 0.0
        self._index_save_interval: float = 5.0  # seconds

        # Index persistence
        self._index_file = self._dir / "message_index.json"

        # Locks for thread safety (lazy-initialised to avoid requiring
        # a running event loop at construction time; see base.py pattern).
        self._chats_lock: asyncio.Lock | None = None
        self._message_locks = LRULockCache(max_size=MAX_LRU_CACHE_SIZE)
        self._index_lock: asyncio.Lock | None = None

        # Bounded pool of open file handles for message JSONL appends.
        # Prevents OS file-descriptor exhaustion under extreme concurrency.
        self._file_pool = _FileHandlePool(max_size=MAX_FILE_HANDLES)

        # Cached path resolution: avoids repeated sanitize + validate + Path
        # construction on every DB operation for the same chat_id.
        self._message_file_cache: LRUDict = LRUDict(max_size=MAX_LRU_CACHE_SIZE)

        self._initialized = False

        # Recovery tracking
        self._last_recovery: Optional[RecoveryResult] = None

        # Per-chat generation counter for write-conflict detection.
        # Incremented on every write (save_message, save_messages_batch).
        # Callers can read the generation before building context and verify
        # it hasn't changed before persisting — preventing interleaved writes
        # from concurrent scheduled and user messages.
        # Bounded via FIFO eviction (oldest entries evicted first) to prevent
        # unbounded memory growth for long-running bots with many chats.
        self._chat_generations: OrderedDict[str, int] = OrderedDict()

        # Optional vector memory store for embedding compression summaries.
        # Set via set_vector_memory() after construction. Enables semantic
        # retrieval of archived conversation history via the memory_recall skill.
        self._vector_memory: Any = None

        # Write circuit breaker: fast-fails write operations when the
        # filesystem is degraded, preventing thundering-herd timeouts from
        # starving the event loop.  Separate from the LLM circuit breaker
        # (different failure domain, different cooldown).
        self._write_breaker = CircuitBreaker(
            failure_threshold=DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
            cooldown_seconds=DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
        )

    def _get_chats_lock(self) -> asyncio.Lock:
        """Return the chats lock, creating it on first use.

        Cannot be eagerly created in __init__ because asyncio.Lock()
        requires a running event loop on Python 3.10+.
        """
        if self._chats_lock is None:
            self._chats_lock = asyncio.Lock()
        return self._chats_lock

    def _get_index_lock(self) -> asyncio.Lock:
        """Return the index lock, creating it on first use.

        Cannot be eagerly created in __init__ because asyncio.Lock()
        requires a running event loop on Python 3.10+.
        """
        if self._index_lock is None:
            self._index_lock = asyncio.Lock()
        return self._index_lock

    async def _run_with_timeout(
        self,
        coro: Any,
        timeout: float,
        operation: str,
    ) -> Any:
        """
        Run a coroutine with a timeout, raising DatabaseError on timeout.

        Args:
            coro: The coroutine to run.
            timeout: Timeout in seconds.
            operation: Operation name for error messages.

        Returns:
            The result of the coroutine.

        Raises:
            DatabaseError: If the operation times out.
        """
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
        coro: Any,
        timeout: float,
        operation: str,
    ) -> Any:
        """Execute a write operation with circuit-breaker protection.

        When the write circuit breaker is OPEN the call is rejected
        immediately (no timeout wait), preventing thundering-herd stalls.
        On success the breaker records a success (closing from HALF_OPEN);
        on any exception the breaker records a failure.
        """
        if await self._write_breaker.is_open():
            log.warning(
                "DB write circuit breaker OPEN — %s rejected", operation,
                extra=_db_log_extra(),
            )
            raise DatabaseError(
                f"Database write circuit breaker open — {operation} rejected",
                operation=operation,
            )
        try:
            result = await self._run_with_timeout(coro, timeout, operation)
            await self._write_breaker.record_success()
            return result
        except Exception:
            await self._write_breaker.record_failure()
            raise

    @property
    def write_breaker(self) -> CircuitBreaker:
        """Public accessor for the write circuit breaker (health / metrics)."""
        return self._write_breaker

    def set_vector_memory(self, vector_memory: Any) -> None:
        """Set the optional vector memory store for embedding compression summaries.

        When set, compression summaries are embedded so the memory_recall skill
        can semantically retrieve archived conversations.  Failures to embed are
        logged but never prevent compression from succeeding.
        """
        self._vector_memory = vector_memory

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def validate_connection(self) -> ValidationResult:
        """
        Validate database connection and file integrity.

        Performs comprehensive checks to ensure the database is accessible
        and all JSON files are valid.

        Returns:
            ValidationResult with validation status, errors, warnings, and details.
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
            except OSError as e:
                errors.append(f"Failed to read chats.json: {e}")
                details["chats_json_valid"] = False
        else:
            details["chats_json_valid"] = True  # Not existing is OK
            details["chats_count"] = 0

        # Check 4: Validate message index integrity
        if self._index_file.exists():
            details["files_checked"].append("message_index.json")
            try:
                content = self._index_file.read_text(encoding="utf-8")
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
            except OSError as e:
                warnings.append(f"Failed to read message_index.json (will be rebuilt): {e}")
                details["message_index_valid"] = False
        else:
            details["message_index_valid"] = True  # Not existing is OK
            details["indexed_message_count"] = 0

        # Check 6: Validate message files (sample check with checksum validation)
        corrupted_files: List[str] = []
        checksum_errors: List[str] = []
        if self._messages_dir.exists():
            msg_files = list(self._messages_dir.glob("*.jsonl"))
            details["message_files_count"] = len(msg_files)
            for msg_file in msg_files[:10]:  # Check first 10 files
                try:
                    content = msg_file.read_text(encoding="utf-8")
                    for line_num, line in enumerate(content.splitlines(), 1):
                        if not line.strip():
                            continue
                        msg = safe_json_parse(line, default=None, log_errors=False, mode=JsonParseMode.LINE)
                        if msg is None:
                            corrupted_files.append(f"{msg_file.name}:{line_num}")
                            continue
                        # Validate checksum if present
                        is_valid, error = validate_checksum(msg)
                        if not is_valid:
                            checksum_errors.append(f"{msg_file.name}:{line_num}")
                except OSError as e:
                    corrupted_files.append(f"{msg_file.name}: {e}")

            if corrupted_files:
                warnings.append(f"Some message files have invalid JSON: {corrupted_files[:3]}")
                details["corrupted_message_files"] = corrupted_files

            if checksum_errors:
                warnings.append(f"Some message files have checksum errors: {checksum_errors[:3]}")
                details["checksum_errors"] = checksum_errors

            # Log corruption detection event
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

    async def connect(self) -> None:
        """
        Initialize storage directory and load existing data.

        Creates the data directory structure if it doesn't exist, loads
        existing chats from disk, and initializes the message ID index
        for duplicate detection.

        This method must be called before any database operations.

        Side Effects:
            - Creates .data/ and .data/messages/ directories if missing
            - Seeds instruction templates from src/templates/instructions/
            - Loads in-memory caches from disk files
            - Sets _initialized flag to True

        Raises:
            OSError: If directory creation fails due to permissions.
        """
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

        # Seed instruction files from templates into workspace/instructions/
        workspace_root = self._dir.parent
        instructions_dir = workspace_root / "instructions"
        template_instructions = Path(__file__).parent.parent / "templates" / "instructions"
        if template_instructions.is_dir():
            instructions_dir.mkdir(parents=True, exist_ok=True)
            for template_file in template_instructions.iterdir():
                if template_file.is_file():
                    target = instructions_dir / template_file.name
                    if not target.exists():
                        shutil.copy2(template_file, target)
                        log.info("Seeded instruction template: %s", target.name)

        # Load message ID index
        await self._ensure_message_index()

        # Migrate existing JSONL files to current schema version
        if self._messages_dir.exists():
            for msg_file in self._messages_dir.glob("*.jsonl"):
                try:
                    await asyncio.to_thread(self._ensure_jsonl_schema, msg_file)
                except Exception as exc:
                    log.warning(
                        "Failed to migrate JSONL schema for %s: %s",
                        msg_file.name,
                        exc,
                    )

        self._initialized = True

    async def close(self) -> None:
        """
        Flush any pending writes and close database.

        Persists any unsaved chat metadata to disk, closes all pooled file
        handles, and marks the database as uninitialized. After calling this
        method, connect() must be called again before any database operations.

        Side Effects:
            - Saves chats index to .data/chats.json
            - Closes all pooled message-file handles
            - Sets _initialized flag to False
        """
        # Flush any debounced chat writes
        if self._chats_dirty:
            await self._save_chats()
        # Flush debounced index writes so the on-disk index is up to date
        if self._index_dirty:
            await self._save_message_index()
            self._index_dirty = False
        # Close all pooled file handles to release OS file descriptors
        self._file_pool.close_all()
        self._initialized = False

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

    async def _get_message_lock(self, chat_id: str) -> asyncio.Lock:
        """
        Get or create a lock for a specific chat's messages.

        Uses LRU cache with bounded size to prevent memory growth.

        Args:
            chat_id: Chat/group ID

        Returns:
            The asyncio.Lock for the given chat_id.
        """
        return await self._message_locks.get_or_create(chat_id)

    # ── persistence helpers ─────────────────────────────────────────────────

    def _check_disk_space_before_write(self, path: Path) -> None:
        """
        Check disk space before write operations.

        Raises DiskSpaceError if insufficient space is available.
        Logs warnings when disk space is low (< 1GB).

        Args:
            path: Directory or file path to check

        Raises:
            DiskSpaceError: If available disk space is below minimum threshold
        """
        try:
            result = check_disk_space(path, min_bytes=DEFAULT_MIN_DISK_SPACE)
            if not result.has_sufficient_space:
                raise DiskSpaceError(
                    f"Insufficient disk space for write operation",
                    path=str(path),
                    free_mb=round(result.free_mb, 2),
                    required_mb=round(DEFAULT_MIN_DISK_SPACE / (1024 * 1024), 2),
                )
        except OSError as e:
            # If we can't check disk space, log warning but proceed
            # (e.g., network drives, permission issues)
            log.warning("Could not verify disk space for %s: %s", path, e)

    # ── JSONL schema migration ──────────────────────────────────────────────

    def _ensure_jsonl_schema(self, file_path: Path) -> None:
        """Ensure a JSONL file has the current schema header.

        Detects headerless (legacy) files and prepends the version header.
        Applies incremental migrations for future schema changes.
        """
        if not file_path.exists() or file_path.stat().st_size == 0:
            return  # Will get header on first write via _append_to_file

        with file_path.open("r", encoding="utf-8") as f:
            first_line = f.readline().strip()

        if not first_line:
            return

        try:
            parsed = json_loads(first_line)
        except Exception:
            return  # Corrupted first line — don't migrate

        if isinstance(parsed, dict) and parsed.get("type") == "header":
            version = parsed.get("_version", 0)
            if version < _JSONL_SCHEMA_VERSION:
                self._apply_jsonl_migrations(file_path, version)
            return

        # No header — legacy file, prepend header
        content = file_path.read_text(encoding="utf-8")
        self._file_pool.invalidate(file_path)
        header = _build_jsonl_header()
        file_path.write_text(header + content, encoding="utf-8")
        log.info(
            "Added JSONL schema v%d header to %s",
            _JSONL_SCHEMA_VERSION,
            file_path.name,
        )

    def _apply_jsonl_migrations(self, file_path: Path, current_version: int) -> None:
        """Apply incremental JSONL schema migrations.

        Each migration in _JSONL_MIGRATIONS is a (target_version, [callables])
        tuple. Callables receive a parsed message dict and return the
        (possibly modified) dict. After all migrations, the file is rewritten
        with the updated header.
        """
        if not _JSONL_MIGRATIONS:
            return  # No migrations defined yet

        content = file_path.read_text(encoding="utf-8")
        lines = content.splitlines()
        migrated: list[str] = []
        header_written = False

        for line in lines:
            if not line.strip():
                continue
            try:
                msg = json_loads(line)
            except Exception:
                migrated.append(line)
                continue

            if msg.get("type") == "header" and not header_written:
                new_header = json_dumps(
                    {"_version": _JSONL_SCHEMA_VERSION, "type": "header"}
                )
                migrated.append(new_header)
                header_written = True
                continue

            for target_ver, fns in _JSONL_MIGRATIONS:
                if current_version < target_ver:
                    for fn in fns:
                        msg = fn(msg)

            migrated.append(json_dumps(msg, ensure_ascii=False))

        if not header_written:
            header = json_dumps({"_version": _JSONL_SCHEMA_VERSION, "type": "header"})
            migrated.insert(0, header)

        self._file_pool.invalidate(file_path)
        file_path.write_text("\n".join(migrated) + "\n", encoding="utf-8")
        log.info(
            "Migrated JSONL schema v%d→v%d for %s",
            current_version,
            _JSONL_SCHEMA_VERSION,
            file_path.name,
        )

    async def _save_chats(self) -> None:
        """
        Atomically save chats index to JSON file.

        Uses atomic write pattern: writes to temp file first, then replaces
        the target file to prevent corruption from partial writes.

        Side Effects:
            - Creates/overwrites .data/chats.json
            - Creates temporary .data/chats.tmp during write
        """
        content = json_dumps(self._chats, indent=2, ensure_ascii=False)
        _db_start = time.monotonic()
        await asyncio.to_thread(self._atomic_write, self._chats_file, content)
        _track_db_latency(time.monotonic() - _db_start)

    def _atomic_write(self, file_path: Path, content: str) -> None:
        """
        Synchronous helper for atomic file writes.

        Checks disk space before writing to prevent corruption from disk full.

        Raises:
            DiskSpaceError: If insufficient disk space available
        """
        from src.utils.async_file import sync_atomic_write

        self._check_disk_space_before_write(file_path)
        sync_atomic_write(file_path, content)

    def _message_file(self, chat_id: str) -> Path:
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

    # ── message index ───────────────────────────────────────────────────────

    async def _ensure_message_index(self) -> None:
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
                    recover_index, self._index_file, self._messages_dir
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

    async def _save_message_index(self) -> None:
        """Persist message ID index to disk."""
        await asyncio.to_thread(
            save_index,
            self._index_file,
            set(self._message_id_index),
            self._atomic_write,
        )

    # ── messages ───────────────────────────────────────────────────────────

    async def message_exists(self, message_id: str) -> bool:
        """Check if a message ID exists (O(1) in-memory lookup)."""
        return message_id in self._message_id_index

    def get_recovery_status(self) -> Optional[RecoveryResult]:
        """Get the last recovery result, if any."""
        return self._last_recovery

    def clear_recovery_status(self) -> None:
        """Clear the recovery status after user notification."""
        self._last_recovery = None

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
            # Invalidate the pooled handle before atomic rewrite
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
                results[chat_id] = await self.repair_message_file(chat_id, backup=True)
            return results

        return await asyncio.to_thread(validate_all_sync, self._messages_dir)

    @staticmethod
    def _build_message_record(
        role: str,
        content: Optional[str],
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> dict:
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
            from src.security.prompt_injection import (
                detect_injection,
                sanitize_user_input,
            )

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

    async def save_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """
        Append a message to the chat's message file.

        Args:
            chat_id: Chat/group ID
            role: Message role ('user', 'assistant', 'tool')
            content: Message content
            name: Optional sender name (for user role) or tool name
            message_id: Optional message ID (generated if not provided)

        Returns:
            The message ID

        Raises:
            DatabaseError: If the operation times out or the write
                circuit breaker is open.
        """
        msg, mid = self._build_message_record(role, content, name, message_id)

        msg_file = self._message_file(chat_id)
        lock = await self._get_message_lock(chat_id)

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

        _track_db_latency(time.monotonic() - _db_start)

        self._bump_generation(chat_id)

        # Trigger compression check (best-effort, non-critical)
        try:
            await self.compress_chat_history(chat_id)
        except Exception:
            log.debug(
                "History compression check failed for %s (non-critical)", chat_id,
                extra=_db_log_extra(chat_id),
            )

        return mid

    async def save_messages_batch(
        self,
        chat_id: str,
        messages: list[dict],
    ) -> list[str]:
        """Persist multiple messages in a single lock acquisition and file append.

        Each dict in *messages* must have ``role`` and ``content`` keys.
        Optional keys: ``name``, ``message_id``.

        Reduces ``asyncio.to_thread()`` hops and JSONL appends from *N*
        individual calls to a single batched write.

        Returns:
            List of message IDs, one per input message.
        """
        records: list[dict] = []
        ids: list[str] = []

        for spec in messages:
            msg, mid = self._build_message_record(
                role=spec["role"],
                content=spec["content"],
                name=spec.get("name"),
                message_id=spec.get("message_id"),
            )
            records.append(msg)
            ids.append(mid)

        msg_file = self._message_file(chat_id)
        lock = await self._get_message_lock(chat_id)

        _db_start = time.monotonic()

        lines = "".join(json_dumps(r, ensure_ascii=False) + "\n" for r in records)

        async def _write_batch():
            async with lock:
                await asyncio.to_thread(self._append_to_file, msg_file, lines)

        await self._guarded_write(
            _write_batch(),
            timeout=DEFAULT_DB_TIMEOUT,
            operation="save_messages_batch",
        )

        await self._update_index(ids)

        _track_db_latency(time.monotonic() - _db_start)

        self._bump_generation(chat_id)

        # Trigger compression check (best-effort, non-critical)
        try:
            await self.compress_chat_history(chat_id)
        except Exception:
            log.debug(
                "History compression check failed for %s (non-critical)", chat_id,
                extra=_db_log_extra(chat_id),
            )

        return ids

    # ── generation counter for write-conflict detection ────────────────────

    def get_generation(self, chat_id: str) -> int:
        """Return the current generation counter for a chat.

        Callers snapshot this before building LLM context and verify with
        ``check_generation()`` before persisting results.  If the generation
        has changed, another writer (scheduled task or concurrent message)
        appended to the history in the meantime.
        """
        return self._chat_generations.get(chat_id, 0)

    def check_generation(self, chat_id: str, expected: int) -> bool:
        """Return True if the chat's generation still matches *expected*."""
        return self._chat_generations.get(chat_id, 0) == expected

    def _bump_generation(self, chat_id: str) -> None:
        """Increment the generation counter after a successful write.

        Uses OrderedDict for deterministic FIFO eviction: when the dict
        exceeds ``MAX_CHAT_GENERATIONS``, the oldest quarter of entries is
        removed to prevent unbounded memory growth in long-running bots.
        """
        self._chat_generations[chat_id] = self._chat_generations.get(chat_id, 0) + 1
        # Move to end so recently-written chats are evicted last (LRU order)
        self._chat_generations.move_to_end(chat_id)

        if len(self._chat_generations) > MAX_CHAT_GENERATIONS:
            discard_count = MAX_CHAT_GENERATIONS // 4
            for _ in range(discard_count):
                self._chat_generations.popitem(last=False)
            log.debug(
                "Trimmed %d entries from _chat_generations (cap=%d)",
                discard_count,
                MAX_CHAT_GENERATIONS,
                extra=_db_log_extra(),
            )

    # ── history compression ─────────────────────────────────────────────────

    def _compressed_summary_file(self, chat_id: str) -> Path:
        """Get the compressed summary file path for a chat."""
        return self._message_file(chat_id).with_suffix(".compressed_summary.json")

    async def get_compressed_summary(self, chat_id: str) -> str | None:
        """Return the compressed history summary text for a chat, if any.

        The summary describes archived messages that were removed during
        compression.  Returns ``None`` when no compression has occurred.
        """
        summary_file = self._compressed_summary_file(chat_id)
        return await asyncio.to_thread(
            self._read_compressed_summary_sync, summary_file,
        )

    @staticmethod
    def _read_compressed_summary_sync(summary_file: Path) -> str | None:
        """Read compressed summary from file (sync, for thread pool)."""
        if not summary_file.exists():
            return None
        try:
            content = summary_file.read_text(encoding="utf-8")
            parsed = json_loads(content)
            if isinstance(parsed, dict):
                return parsed.get("content")
        except Exception:
            pass
        return None

    async def compress_chat_history(self, chat_id: str) -> bool:
        """Compress a chat's history when the JSONL file exceeds the line threshold.

        Archives the oldest messages by replacing them with a summary stored in
        a separate ``.compressed_summary.json`` file.  The most recent messages
        are kept intact in the JSONL.  This reduces disk I/O and reverse-seek
        latency for long-lived conversations.

        Returns:
            ``True`` if compression was performed, ``False`` if skipped.
        """
        msg_file = self._message_file(chat_id)
        if not msg_file.exists():
            return False

        lock = await self._get_message_lock(chat_id)
        async with lock:
            result = await asyncio.to_thread(
                self._compress_chat_history_sync, chat_id, msg_file,
            )

        if result.get("compressed"):
            removed_ids = result.get("removed_ids", [])
            if removed_ids:
                async with self._get_index_lock():
                    for mid in removed_ids:
                        self._message_id_index.pop(mid, None)
                    self._index_dirty = True

            # Best-effort: embed compression summary for semantic retrieval
            summary_text = result.get("summary_text")
            if summary_text and self._vector_memory is not None:
                try:
                    await self._vector_memory.save(
                        chat_id, summary_text, category="compression_summary",
                    )
                except Exception:
                    log.debug(
                        "Failed to embed compression summary for %s (non-critical)",
                        chat_id,
                        extra=_db_log_extra(chat_id),
                    )

        return bool(result.get("compressed"))

    def _compress_chat_history_sync(
        self, chat_id: str, msg_file: Path,
    ) -> dict:
        """Synchronous compression logic (runs in thread pool).

        Returns a dict with ``compressed`` (bool) and ``removed_ids`` (list).
        """
        # Quick size gate: skip if file is too small to have threshold lines.
        # Minimum ~200 bytes per JSONL line is a conservative estimate.
        try:
            file_size = msg_file.stat().st_size
        except FileNotFoundError:
            log.debug("compress: file disappeared before stat — %s [%s]", msg_file.name, chat_id)
            return {"compressed": False}
        except OSError:
            log.warning("compress: I/O error during stat — %s [%s]", msg_file.name, chat_id)
            return {"compressed": False}

        if file_size < COMPRESSION_LINE_THRESHOLD * 200:
            return {"compressed": False}

        # Read the file
        try:
            content = msg_file.read_text(encoding="utf-8")
        except FileNotFoundError:
            log.debug("compress: file disappeared before read — %s [%s]", msg_file.name, chat_id)
            return {"compressed": False}
        except OSError:
            log.warning("compress: I/O error during read — %s [%s]", msg_file.name, chat_id)
            return {"compressed": False}

        lines = content.splitlines()

        # Separate header from message lines
        header_line: str | None = None
        msg_lines: list[str] = []
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            if i == 0 and header_line is None:
                try:
                    parsed = json_loads(line)
                    if isinstance(parsed, dict) and parsed.get("type") == "header":
                        header_line = line
                        continue
                except Exception:
                    pass
            msg_lines.append(line)

        if len(msg_lines) <= COMPRESSION_LINE_THRESHOLD:
            return {"compressed": False}

        # Split into old (to archive) and recent (to keep)
        compress_count = len(msg_lines) - COMPRESSION_KEEP_RECENT
        old_lines = msg_lines[:compress_count]
        recent_lines = msg_lines[compress_count:]

        # Extract metadata from old messages
        import datetime

        first_ts: float | None = None
        last_ts: float | None = None
        user_count = 0
        assistant_count = 0
        removed_ids: list[str] = []

        for line in old_lines:
            try:
                msg = json_loads(line)
            except Exception:
                continue

            msg_id = msg.get("id")
            if msg_id:
                removed_ids.append(msg_id)

            ts = msg.get("timestamp")
            if ts is not None:
                if first_ts is None or ts < first_ts:
                    first_ts = ts
                if last_ts is None or ts > last_ts:
                    last_ts = ts

            role = msg.get("role")
            if role == "user":
                user_count += 1
            elif role == "assistant":
                assistant_count += 1

        # Accumulate with any prior compression summary
        summary_file = msg_file.with_suffix(".compressed_summary.json")
        total_removed = len(old_lines)
        total_user = user_count
        total_assistant = assistant_count

        if summary_file.exists():
            try:
                existing = json_loads(summary_file.read_text(encoding="utf-8"))
                if isinstance(existing, dict):
                    meta = existing.get("_metadata", {})
                    total_removed += meta.get("messages_removed", 0)
                    total_user += meta.get("user_messages", 0)
                    total_assistant += meta.get("assistant_messages", 0)
                    existing_first = meta.get("first_timestamp")
                    existing_last = meta.get("last_timestamp")
                    if existing_first is not None and (
                        first_ts is None or existing_first < first_ts
                    ):
                        first_ts = existing_first
                    if existing_last is not None and (
                        last_ts is None or existing_last > last_ts
                    ):
                        last_ts = existing_last
            except Exception:
                pass

        # Format date range (UTC for consistency)
        date_range = ""
        if first_ts is not None and last_ts is not None:
            start = datetime.datetime.fromtimestamp(
                first_ts, tz=datetime.timezone.utc,
            ).strftime("%Y-%m-%d")
            end = datetime.datetime.fromtimestamp(
                last_ts, tz=datetime.timezone.utc,
            ).strftime("%Y-%m-%d")
            date_range = f" from {start} to {end}"

        summary_text = (
            f"📋 [Conversation History Compressed] "
            f"{total_removed} messages "
            f"({total_user} user, {total_assistant} assistant)"
            f"{date_range} have been archived. "
            f"The current conversation continues from the most recent messages. "
            f"Use memory_recall if you need to reference specific past interactions."
        )

        # Write summary file (small, fast — do this before truncating JSONL)
        summary_data = {
            "content": summary_text,
            "_metadata": {
                "messages_removed": total_removed,
                "user_messages": total_user,
                "assistant_messages": total_assistant,
                "first_timestamp": first_ts,
                "last_timestamp": last_ts,
                "last_compressed": time.time(),
            },
        }
        try:
            summary_file.write_text(
                json_dumps(summary_data, ensure_ascii=False), encoding="utf-8",
            )
        except OSError as exc:
            log.warning(
                "Failed to write compression summary for %s: %s", chat_id, exc,
                extra=_db_log_extra(chat_id),
            )
            return {"compressed": False}

        # Build new JSONL: header + recent messages
        new_lines: list[str] = []
        if header_line:
            new_lines.append(header_line)
        new_lines.extend(recent_lines)

        # Invalidate pooled handle before rewrite
        self._file_pool.invalidate(msg_file)

        # Write the truncated file atomically
        new_content = "\n".join(new_lines) + "\n"
        try:
            self._atomic_write(msg_file, new_content)
        except OSError as exc:
            log.warning(
                "Failed to write compressed JSONL for %s: %s", chat_id, exc,
                extra=_db_log_extra(chat_id),
            )
            return {"compressed": False}

        log.info(
            "Compressed chat history for %s: %d old messages archived, "
            "%d recent messages kept (file: %s)",
            chat_id,
            compress_count,
            len(recent_lines),
            msg_file.name,
            extra=_db_log_extra(chat_id),
        )

        return {
            "compressed": True,
            "removed_ids": removed_ids,
            "summary_text": summary_text,
        }

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
                await self._save_message_index()
                self._last_index_save = now
                self._index_dirty = False

    def _append_to_file(self, file_path: Path, content: str) -> None:
        """
        Append content to a file using a pooled file handle.

        Uses the file-handle pool to avoid repeated open/close syscalls and
        prevent OS file-descriptor exhaustion under extreme concurrency.
        Line-buffered handles flush on every newline automatically.

        Automatically prepends a JSONL schema version header on new/empty files.

        Raises:
            DiskSpaceError: If insufficient disk space available
        """
        self._check_disk_space_before_write(file_path)
        # Prepend version header on new/empty files
        if not file_path.exists() or file_path.stat().st_size == 0:
            content = _build_jsonl_header() + content
        handle = self._file_pool.get_or_open(file_path)
        handle.write(content)

    async def get_recent_messages(self, chat_id: str, limit: int = 50) -> List[dict]:
        """
        Return the last *limit* messages for a chat, oldest first.

        Validates checksums on read and logs any corruption detected.
        Corruption does not crash the application - corrupted messages are skipped.

        Note: Maximum 500 messages can be retrieved (hard cap for memory safety).

        Args:
            chat_id: Chat/group ID
            limit: Maximum number of messages to return (capped at 500)

        Returns:
            List of message dicts with 'role', 'content', 'name' keys
        """
        # Enforce hard cap to prevent memory issues
        limit = min(limit, MAX_MESSAGE_HISTORY)

        msg_file = self._message_file(chat_id)

        if not msg_file.exists():
            return []

        lock = await self._get_message_lock(chat_id)
        _db_start = time.monotonic()

        async def _read_messages():
            async with lock:
                try:
                    # Run file I/O in thread pool to avoid blocking
                    # Pass limit directly to avoid reading more lines than needed
                    lines = await asyncio.to_thread(self._read_file_lines, msg_file, limit)
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
            log.warning("Timeout reading messages for chat %s", chat_id,
                        extra=_db_log_extra(chat_id))
            return []

        # Lines are already limited to the last N by _read_file_lines
        recent_lines = lines
        messages = []
        corruption_detected = False

        for line_num_offset, line in enumerate(recent_lines):
            msg = safe_json_parse(line, default=None, log_errors=False, mode=JsonParseMode.LINE)
            if msg is None:
                if line.strip():  # Only log if line wasn't empty
                    actual_line_num = len(lines) - len(recent_lines) + line_num_offset + 1
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
                # Log corruption detection event
                actual_line_num = len(lines) - len(recent_lines) + line_num_offset + 1
                log.warning(
                    "Checksum validation failed for chat %s line %d: %s",
                    chat_id,
                    actual_line_num,
                    error,
                    extra=_db_log_extra(chat_id),
                )
                corruption_detected = True
                # Skip corrupted message - recovery mechanism
                continue

            messages.append(
                {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "name": msg.get("name"),
                    "_sanitized": msg.get("_sanitized", False),
                }
            )

        # Log summary if corruption was detected
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

    # Maximum chunks the reverse-seek will scan before falling back to a
    # simple deque read.  Prevents unbounded looping on corrupted files that
    # lack newline characters.
    _MAX_SEEK_ITERATIONS = 10_000

    def _read_file_lines(self, file_path: Path, limit: int = MAX_MESSAGE_HISTORY) -> List[str]:
        """Read the last N lines from a file without reading the entire file.

        For small files (<64KB), reads normally using deque for O(limit) memory.
        For larger files, uses mmap-based reverse-seek to find line boundaries,
        letting the OS manage page-level access without loading the entire file
        into Python memory. Falls back to deque read if mmap is unavailable.

        If the reverse-seek exceeds ``_MAX_SEEK_ITERATIONS`` chunks (e.g. a
        corrupted file with very few newlines), it falls back to a simple
        deque read to avoid an unbounded loop.
        """
        file_size = file_path.stat().st_size

        # For small files, simple read is faster (avoids seek overhead)
        if file_size < 65_536:
            with file_path.open("r", encoding="utf-8") as f:
                return [line.rstrip("\r") for line in deque(f, maxlen=limit)]

        target_newlines = limit + 1  # +1 because last line may not end with \n
        newline_count = 0
        iterations = 0
        pos = file_size

        with file_path.open("rb") as f:
            try:
                mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            except (OSError, ValueError):
                # mmap unavailable (special filesystem, empty mapping) — fall back
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
                            return [line.rstrip("\r") for line in deque(fb, maxlen=limit)]

                    # Scan a region of up to 8192 bytes backwards through
                    # the mmap. The OS manages page-level access so only
                    # touched pages are loaded into memory.
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

        # Read just the needed region as text
        with file_path.open("r", encoding="utf-8") as f:
            f.seek(max(pos, 0))
            remaining_text = f.read()

        # Split and take last N lines, preserving order (oldest first)
        all_lines = remaining_text.splitlines()
        selected = all_lines[-limit:] if len(all_lines) > limit else all_lines
        return [line.rstrip("\r") for line in selected]

    # ── chats ──────────────────────────────────────────────────────────────

    async def upsert_chat(self, chat_id: str, name: Optional[str] = None) -> None:
        """
        Create or update a chat's metadata.

        Uses debounced saving: only writes to disk after _chats_save_interval
        seconds since the last save, or on close(). This avoids writing
        chats.json on every single message.

        Args:
            chat_id: Unique chat/group identifier.
            name: Optional sender/group name for display purposes.

        Side Effects:
            - Modifies _chats in-memory cache
            - May persist changes to .data/chats.json (debounced)

        Raises:
            DatabaseError: If the operation times out.
        """
        now = time.time()

        async def _upsert():
            async with self._get_chats_lock():
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
                # Only flush to disk if enough time has passed since last save
                if (now - self._last_chats_save) >= self._chats_save_interval:
                    await self._save_chats()
                    self._last_chats_save = now
                    self._chats_dirty = False

        await self._guarded_write(
            _upsert(),
            timeout=DEFAULT_DB_TIMEOUT,
            operation="upsert_chat",
        )

    async def flush_chats(self) -> None:
        """Force-flush dirty chat metadata to disk."""
        async with self._get_chats_lock():
            if self._chats_dirty:
                await self._save_chats()
                self._last_chats_save = time.time()
                self._chats_dirty = False

    async def list_chats(self) -> List[dict]:
        """
        List all chats sorted by last_active (most recent first).

        Returns:
            List of chat dicts with 'chat_id', 'name', 'created_at', 'last_active'
        """
        async with self._get_chats_lock():
            chats = [
                {
                    "chat_id": chat_id,
                    **chat_data,
                }
                for chat_id, chat_data in self._chats.items()
            ]

        # Sort by last_active descending
        chats.sort(key=lambda x: x.get("last_active", 0), reverse=True)
        return chats


# ─────────────────────────────────────────────────────────────────────────────
# Async Context Manager for Database lifecycle
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def get_database(data_dir: str) -> AsyncIterator[Database]:
    """
    Async context manager for database lifecycle management.

    Automatically handles connect() on entry and close() on exit,
    ensuring proper resource cleanup even on exceptions.

    Usage:
        async with get_database(".data") as db:
            await db.save_message(chat_id, "user", "Hello")
            messages = await db.get_recent_messages(chat_id)

    Args:
        data_dir: Path to directory for storing data files.

    Yields:
        Database: Connected database instance.

    Raises:
        OSError: If directory creation fails due to permissions.
    """
    db = Database(data_dir)
    try:
        await db.connect()
        yield db
    finally:
        await db.close()
