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
import re
import shutil
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

from src.constants import (
    DB_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    DB_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    DEFAULT_DB_TIMEOUT,
    MAX_CHAT_GENERATIONS,
    MAX_FILE_HANDLES,
    MAX_LRU_CACHE_SIZE,
)
from src.utils.circuit_breaker import CircuitBreaker
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
    MAX_MESSAGE_ID_INDEX,
    _build_jsonl_header,
    _db_log_extra,
    _sanitize_name,
    _track_db_latency,
    _track_db_write_latency,
    _validate_chat_id,
)
from src.db.file_pool import FileHandlePool
from src.db.message_store import MessageStore
from src.exceptions import DatabaseError, DiskSpaceError, ErrorCode
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
from src.utils.path import sanitize_path_component as _sanitize_chat_id_for_path

log = logging.getLogger(__name__)

# ── backward-compatible re-exports ────────────────────────────────────────

# Re-export so ``from src.db import CorruptionResult`` keeps working.
# Also re-export the type alias for legacy code that references the
# internal _FileHandlePool name.
from src.db.file_pool import FileHandlePool as _FileHandlePool  # noqa: F401

__all__ = [
    "ValidationResult",
    "RecoveryResult",
    "CorruptionResult",
    "Database",
    "get_database",
    "_validate_chat_id",
    "_sanitize_chat_id_for_path",
]


# ── constants (module-level for backward compat) ──────────────────────────

# JSONL schema version for forward-compatible message format changes.
_JSONL_SCHEMA_VERSION = 1
_JSONL_MIGRATIONS: list[tuple[int, list[Any]]] = []

# Maximum messages that can be retrieved in a single query (memory safety)
MAX_MESSAGE_HISTORY = 500


@dataclass
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

        # Locks for thread safety (lazy-initialised to avoid requiring
        # a running event loop at construction time).
        self._chats_lock: asyncio.Lock | None = None
        self._message_locks = LRULockCache(max_size=MAX_LRU_CACHE_SIZE)

        # Bounded pool of open file handles for message JSONL appends.
        self._file_pool = FileHandlePool(max_size=MAX_FILE_HANDLES)

        # Cached path resolution: avoids repeated sanitize + validate + Path
        # construction on every DB operation for the same chat_id.
        self._message_file_cache: LRUDict = LRUDict(max_size=MAX_LRU_CACHE_SIZE)

        self._initialized = False

        # Per-chat generation counter for write-conflict detection.
        self._chat_generations: OrderedDict[str, int] = OrderedDict()

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
            mark_index_dirty_fn=lambda: setattr(
                self._message_store, "_index_dirty", True
            ),
            atomic_write_fn=self._atomic_write,
            vector_memory=None,
        )

    # ── lock accessors ────────────────────────────────────────────────────

    def _get_chats_lock(self) -> asyncio.Lock:
        """Return the chats lock, creating it on first use."""
        if self._chats_lock is None:
            self._chats_lock = asyncio.Lock()
        return self._chats_lock

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
        coro: Any,
        timeout: float,
        operation: str,
    ) -> Any:
        """Execute a write operation with circuit-breaker protection."""
        if self._write_breaker.is_open():
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
            self._write_breaker.record_success()
            return result
        except Exception:
            self._write_breaker.record_failure()
            raise

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
            except OSError as e:
                errors.append(f"Failed to read chats.json: {e}")
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
            except OSError as e:
                warnings.append(f"Failed to read message_index.json (will be rebuilt): {e}")
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
                        msg = safe_json_parse(line, default=None, log_errors=False, mode=JsonParseMode.LINE)
                        if msg is None:
                            corrupted_files.append(f"{msg_file.name}:{line_num}")
                            continue
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
        if template_instructions.is_dir():
            instructions_dir.mkdir(parents=True, exist_ok=True)
            for template_file in template_instructions.iterdir():
                if template_file.is_file():
                    target = instructions_dir / template_file.name
                    if not target.exists():
                        shutil.copy2(template_file, target)
                        log.info("Seeded instruction template: %s", target.name)

        # Load message ID index (delegated to MessageStore)
        await self._message_store.ensure_message_index()

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
        """Flush any pending writes and close database."""
        # Flush any debounced chat writes
        if self._chats_dirty:
            await self._save_chats()
        # Flush debounced index writes via MessageStore
        if self._message_store.index_dirty:
            await self._message_store.save_message_index()
            self._message_store.index_dirty = False
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

    # ── JSONL schema migration ──────────────────────────────────────────────

    def _ensure_jsonl_schema(self, file_path: Path) -> None:
        """Ensure a JSONL file has the current schema header."""
        if not file_path.exists() or file_path.stat().st_size == 0:
            return

        with file_path.open("r", encoding="utf-8") as f:
            first_line = f.readline().strip()

        if not first_line:
            return

        try:
            parsed = json_loads(first_line)
        except Exception:
            return

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
        """Apply incremental JSONL schema migrations."""
        if not _JSONL_MIGRATIONS:
            return

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

    # ── chats persistence ─────────────────────────────────────────────────

    async def _save_chats(self) -> None:
        """Atomically save chats index to JSON file."""
        content = json_dumps(self._chats, indent=2, ensure_ascii=False)
        _db_start = time.monotonic()
        await asyncio.to_thread(self._atomic_write, self._chats_file, content)
        elapsed = time.monotonic() - _db_start
        _track_db_latency(elapsed)
        _track_db_write_latency(elapsed)

    async def upsert_chat(self, chat_id: str, name: Optional[str] = None) -> None:
        """Create or update a chat's metadata (debounced save)."""
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
        """List all chats sorted by last_active (most recent first)."""
        async with self._get_chats_lock():
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
        return self._chat_generations.get(chat_id, 0)

    def check_generation(self, chat_id: str, expected: int) -> bool:
        """Return True if the chat's generation still matches *expected*."""
        return self._chat_generations.get(chat_id, 0) == expected

    def _bump_generation(self, chat_id: str) -> None:
        """Increment the generation counter after a successful write."""
        self._chat_generations[chat_id] = self._chat_generations.get(chat_id, 0) + 1
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
