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
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from src.constants import (
    DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    DB_WRITE_MAX_RETRIES,
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
from src.db.file_pool import FileHandlePool, ReadHandlePool
from src.db.generations import GenerationCounter
from src.db.message_store import MessageStore
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


@dataclass(slots=True)
class ValidationResult:
    """Result of database connection validation."""

    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    details: Dict[str, Any] = field(default_factory=dict)


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

        # In-memory caches
        self._chats: Dict[str, Dict[str, Any]] = {}

        # Debounce chat saves: only flush to disk after dirty interval
        self._chats_dirty: bool = False
        self._last_chats_save: float = 0.0
        self._chats_save_interval: float = 5.0  # seconds

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
        """Execute a write with retry and circuit-breaker protection.

        *write_fn* is a zero-arg async callable (factory) so the write can
        be retried on transient ``OSError`` (disk-full, lock-contention)
        with exponential backoff + jitter.  Only after all retries are
        exhausted is a failure recorded on the circuit breaker.
        """
        if await self._write_breaker.is_open():
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
            try:
                result = await self._run_with_timeout(
                    write_fn(),
                    timeout,
                    operation,
                )
                await self._write_breaker.record_success()
                return result
            except DatabaseError:
                await self._write_breaker.record_failure()
                raise  # timeout — never retry
            except OSError as exc:
                if attempt >= DB_WRITE_MAX_RETRIES:
                    log.warning(
                        "DB write retry exhausted after %d attempts for %s: %s",
                        DB_WRITE_MAX_RETRIES + 1,
                        operation,
                        exc,
                        extra=_db_log_extra(),
                    )
                    await self._write_breaker.record_failure()
                    raise
                actual_delay = calculate_delay_with_jitter(delay)
                log.info(
                    "Transient DB write error for %s, retrying attempt %d/%d after %.2fs: %s",
                    operation,
                    attempt + 1,
                    DB_WRITE_MAX_RETRIES,
                    actual_delay,
                    exc,
                    extra=_db_log_extra(),
                )
                await asyncio.sleep(actual_delay)
                delay *= 2

    @property
    def write_breaker(self) -> CircuitBreaker:
        """Public accessor for the write circuit breaker (health / metrics)."""
        return self._write_breaker

    # ── disk / atomic write helpers ───────────────────────────────────────

    def _check_disk_space_before_write(self, path: Path) -> None:
        """Check disk space before write operations. Raises DiskSpaceError if low."""
        from src.db.db_utils import _check_disk_space_before_write as _impl

        _impl(path)

    def _atomic_write(self, file_path: Path, content: str) -> None:
        """Synchronous helper for atomic file writes."""
        from src.db.db_utils import _atomic_write as _impl

        _impl(file_path, content)

    # ── vector memory ─────────────────────────────────────────────────────

    def set_vector_memory(self, vector_memory: Any) -> None:
        """Set the optional vector memory store for embedding compression summaries."""
        self._vector_memory = vector_memory
        self._compression.set_vector_memory(vector_memory)

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def validate_connection(self) -> ValidationResult:
        """Validate database connection and file integrity."""
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
        torn down and ``asyncio.to_thread`` raises RuntimeError.
        """
        from src.utils.json_utils import json_dumps

        content = json_dumps(self._chats, indent=2, ensure_ascii=False)
        self._atomic_write(self._chats_file, content)
        self._chats_dirty = False
        log.info("Saved chats via synchronous fallback during shutdown")

    async def close(self) -> None:
        """Flush any pending writes and close database.

        Each flush is wrapped individually so that a failure in one does
        not prevent the others or the handle-pool cleanup from running.
        Failures are logged as warnings rather than propagated so that
        ``close()`` always releases OS file descriptors.
        """
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
        """Atomically save chats index to JSON file.

        Wraps the disk write through ``_guarded_write`` so transient
        ``OSError`` (disk-full, lock-contention) are retried with
        exponential backoff before tripping the write circuit breaker.
        """
        content = json_dumps(self._chats, indent=2, ensure_ascii=False)
        _db_start = time.monotonic()

        async def _write_chats():
            try:
                await asyncio.to_thread(self._atomic_write, self._chats_file, content)
            except RuntimeError as exc:
                # During teardown, the loop default executor can already be
                # shutting down. Keep close() best-effort by falling back to a
                # direct synchronous write of the small chats index.
                if "cannot schedule new futures after shutdown" not in str(exc):
                    raise
                log.warning(
                    "Default executor unavailable while saving chats; using synchronous fallback"
                )
                self._atomic_write(self._chats_file, content)

        await self._guarded_write(
            _write_chats,
            timeout=DEFAULT_DB_TIMEOUT,
            operation="save_chats",
        )

        elapsed = time.monotonic() - _db_start
        _track_db_latency(elapsed)
        _track_db_write_latency(elapsed)

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
                if (now - self._last_chats_save) >= self._chats_save_interval:
                    await self._save_chats()
                    self._last_chats_save = now
                    self._chats_dirty = False

        await self._guarded_write(
            _upsert,
            timeout=DEFAULT_DB_TIMEOUT,
            operation="upsert_chat",
        )

    async def upsert_chat_and_save_message(
        self,
        chat_id: str,
        sender_name: str,
        role: str,
        content: str,
        name: Optional[str] = None,
        message_id: Optional[str] = None,
    ) -> str:
        """Upsert chat metadata and persist a message in one logical operation.

        Performs the lightweight in-memory chat update inline (no
        ``_guarded_write``) and delegates only the message write through the
        guarded pipeline, reducing circuit-breaker checks and retry overhead
        from two calls to one.
        """
        now = time.time()
        async with self._chats_lock:
            if chat_id in self._chats:
                self._chats[chat_id]["last_active"] = now
                if sender_name:
                    self._chats[chat_id]["name"] = sender_name
            else:
                self._chats[chat_id] = {
                    "name": sender_name,
                    "created_at": now,
                    "last_active": now,
                    "metadata": {},
                }
            self._chats_dirty = True

        return await self.save_message(
            chat_id=chat_id,
            role=role,
            content=content,
            name=name,
            message_id=message_id,
        )

    async def flush_chats(self) -> None:
        """Force-flush dirty chat metadata to disk."""
        async with self._chats_lock:
            if self._chats_dirty:
                await self._save_chats()
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

        Delegates to ``MessageStore.get_recent_messages``.
        """
        return await self._message_store.get_recent_messages(chat_id, limit)

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
