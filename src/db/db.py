"""
db.py — File-based persistence layer (async, no external dependencies).

Storage structure:
    workspace/
    └── .data/
        ├── chats.json          # Chat metadata index
        └── messages/
            ├── <chat_id_1>.jsonl   # Messages (JSONL = one JSON per line)
            └── <chat_id_2>.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Dict, List, Optional, Any

from src.constants import DEFAULT_DB_TIMEOUT, MAX_LRU_CACHE_SIZE
from src.utils import (
    LRULockCache,
    check_disk_space,
    DEFAULT_MIN_DISK_SPACE,
    safe_json_parse,
    safe_json_parse_line,
    safe_json_parse_with_error,
)
from src.exceptions import DatabaseError, DiskSpaceError

from src.db.db_integrity import (
    CorruptionResult,
    MessageLine,
    calculate_checksum,
    validate_checksum,
    detect_corruption_sync,
    backup_file_sync,
    repair_file_sync,
    validate_all_sync,
)
from src.db.db_index import (
    RecoveryResult,
    load_index,
    save_index,
    rebuild_index,
    recover_index,
)

log = logging.getLogger(__name__)

# Maximum messages that can be retrieved in a single query (memory safety)
MAX_MESSAGE_HISTORY = 500

# Maximum entries in the message ID index (prevents unbounded memory growth)
MAX_MESSAGE_ID_INDEX = 100_000

# Pattern for valid chat_id (safe for file paths)
_CHAT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\.\@]+$")

# Characters that need sanitization for file paths
_SANITIZE_CHARS = {
    "@": "_at_",
    ":": "_col_",
    "/": "_sl_",
    "\\": "_bs_",
    "|": "_pi_",
    "?": "_qm_",
    "*": "_as_",
    "<": "_lt_",
    ">": "_gt_",
    '"': "_dq_",
}


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


def _sanitize_chat_id_for_path(chat_id: str) -> str:
    """
    Sanitize chat_id for use in file paths.

    Replaces characters that are problematic on some file systems
    (Windows, especially) with safe alternatives.

    Args:
        chat_id: The chat ID to sanitize.

    Returns:
        Sanitized chat_id safe for use in file paths.
    """
    sanitized = chat_id
    for char, replacement in _SANITIZE_CHARS.items():
        sanitized = sanitized.replace(char, replacement)
    return sanitized


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

        # Index persistence
        self._index_file = self._dir / "message_index.json"

        # Locks for thread safety
        self._chats_lock = asyncio.Lock()
        self._message_locks = LRULockCache(max_size=MAX_LRU_CACHE_SIZE)
        self._index_lock = asyncio.Lock()

        self._initialized = False

        # Recovery tracking
        self._last_recovery: Optional[RecoveryResult] = None

    async def _run_with_timeout(self, coro, timeout: float, operation: str) -> Any:
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
            )
            raise DatabaseError(
                f"Operation timed out after {timeout}s",
                operation=operation,
                timeout=timeout,
            )

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
                errors.append(
                    f"Messages directory is not writable: {self._messages_dir}"
                )
                details["messages_dir_writable"] = False
            else:
                details["messages_dir_writable"] = True

        # Check 3: Validate chats.json
        if self._chats_file.exists():
            details["files_checked"].append("chats.json")
            try:
                content = self._chats_file.read_text(encoding="utf-8")
                result = safe_json_parse_with_error(content, expected_type=dict)
                if not result.success:
                    if result.error_type == "type":
                        errors.append("chats.json is not a valid JSON object")
                    else:
                        errors.append(f"chats.json is corrupted: {result.error}")
                    details["chats_json_valid"] = False
                else:
                    details["chats_json_valid"] = True
                    details["chats_count"] = len(result.data)
            except Exception as e:
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
                result = safe_json_parse_with_error(content, expected_type=list)
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
            except Exception as e:
                warnings.append(
                    f"Failed to read message_index.json (will be rebuilt): {e}"
                )
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
                        msg = safe_json_parse_line(line, default=None, log_errors=False)
                        if msg is None:
                            corrupted_files.append(f"{msg_file.name}:{line_num}")
                            continue
                        # Validate checksum if present
                        is_valid, error = validate_checksum(msg)
                        if not is_valid:
                            checksum_errors.append(f"{msg_file.name}:{line_num}")
                except Exception as e:
                    corrupted_files.append(f"{msg_file.name}: {e}")

            if corrupted_files:
                warnings.append(
                    f"Some message files have invalid JSON: {corrupted_files[:3]}"
                )
                details["corrupted_message_files"] = corrupted_files

            if checksum_errors:
                warnings.append(
                    f"Some message files have checksum errors: {checksum_errors[:3]}"
                )
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
        self._chats = (
            safe_json_parse(
                self._chats_file.read_text(encoding="utf-8")
                if self._chats_file.exists()
                else "{}",
                default={},
                expected_type=dict,
                log_errors=True,
            )
            if self._chats_file.exists()
            else {}
        )

        # Seed instruction files from templates into workspace/instructions/
        workspace_root = self._dir.parent
        instructions_dir = workspace_root / "instructions"
        template_instructions = (
            Path(__file__).parent.parent / "templates" / "instructions"
        )
        if template_instructions.is_dir():
            instructions_dir.mkdir(parents=True, exist_ok=True)
            for template_file in template_instructions.iterdir():
                if template_file.is_file():
                    target = instructions_dir / template_file.name
                    if not target.exists():
                        import shutil

                        shutil.copy2(template_file, target)
                        log.info("Seeded instruction template: %s", target.name)

        # Load message ID index
        await self._ensure_message_index()

        self._initialized = True

    async def close(self) -> None:
        """
        Flush any pending writes and close database.

        Persists any unsaved chat metadata to disk and marks the database
        as uninitialized. After calling this method, connect() must be called
        again before any database operations.

        Side Effects:
            - Saves chats index to .data/chats.json
            - Sets _initialized flag to False
        """
        # Flush any debounced chat writes
        if self._chats_dirty:
            await self._save_chats()
        self._initialized = False

    async def __aenter__(self) -> "Database":
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
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

    async def _save_chats(self) -> None:
        """
        Atomically save chats index to JSON file.

        Uses atomic write pattern: writes to temp file first, then replaces
        the target file to prevent corruption from partial writes.

        Side Effects:
            - Creates/overwrites .data/chats.json
            - Creates temporary .data/chats.tmp during write
        """
        content = json.dumps(self._chats, indent=2, ensure_ascii=False)
        await asyncio.to_thread(self._atomic_write, self._chats_file, content)

    def _atomic_write(self, file_path: Path, content: str) -> None:
        """
        Synchronous helper for atomic file writes.

        Checks disk space before writing to prevent corruption from disk full.

        Raises:
            DiskSpaceError: If insufficient disk space available
        """
        self._check_disk_space_before_write(file_path)
        temp_file = file_path.with_suffix(".tmp")
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(file_path)

    def _message_file(self, chat_id: str) -> Path:
        """Get the message file path for a chat."""
        _validate_chat_id(chat_id)
        # Use comprehensive sanitization for cross-platform file path safety
        safe_id = _sanitize_chat_id_for_path(chat_id)
        return self._messages_dir / f"{safe_id}.jsonl"

    # ── message index ───────────────────────────────────────────────────────

    async def _ensure_message_index(self) -> None:
        """Load or rebuild message ID index from disk."""
        async with self._index_lock:
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

    async def repair_message_file(
        self, chat_id: str, backup: bool = True
    ) -> CorruptionResult:
        """Detect and repair corruption, optionally backing up first."""
        result = await self.detect_corruption(chat_id)
        if not result.is_corrupted:
            return result

        if backup:
            result.backup_path = await self.backup_corrupted_file(chat_id)

        msg_file = self._message_file(chat_id)
        lock = await self._get_message_lock(chat_id)
        async with lock:
            result.repaired = await asyncio.to_thread(
                repair_file_sync, msg_file, result, self._atomic_write
            )
        return result

    async def validate_all_message_files(
        self, repair: bool = False
    ) -> Dict[str, CorruptionResult]:
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
            DatabaseError: If the operation times out.
        """
        mid = message_id or str(uuid.uuid4())
        timestamp = time.time()

        # Calculate checksum for corruption detection
        checksum = calculate_checksum(content, role, timestamp)

        msg = {
            "id": mid,
            "role": role,
            "content": content,
            "name": name,
            "timestamp": timestamp,
            "_checksum": checksum,
        }

        msg_file = self._message_file(chat_id)
        lock = await self._get_message_lock(chat_id)

        async def _write_message():
            async with lock:
                # Run file I/O in thread pool to avoid blocking
                await asyncio.to_thread(
                    self._append_to_file,
                    msg_file,
                    json.dumps(msg, ensure_ascii=False) + "\n",
                )

        await self._run_with_timeout(
            _write_message(),
            timeout=DEFAULT_DB_TIMEOUT,
            operation="save_message",
        )

        # Add to in-memory index for O(1) lookups (with size cap)
        async with self._index_lock:
            self._message_id_index[mid] = None
            if len(self._message_id_index) > MAX_MESSAGE_ID_INDEX:
                # Deterministic FIFO eviction: remove oldest entries first
                # dict preserves insertion order in Python 3.7+
                discard_count = MAX_MESSAGE_ID_INDEX // 4
                for _ in range(discard_count):
                    # popitem(last=False) removes the first (oldest) entry
                    self._message_id_index.popitem(last=False)
                log.warning(
                    "Trimmed %d entries from message ID index (cap=%d)",
                    discard_count,
                    MAX_MESSAGE_ID_INDEX,
                )

        return mid

    def _append_to_file(self, file_path: Path, content: str) -> None:
        """
        Synchronous helper to append content to a file.

        Checks disk space before writing to prevent corruption from disk full.

        Raises:
            DiskSpaceError: If insufficient disk space available
        """
        self._check_disk_space_before_write(file_path)
        with file_path.open("a", encoding="utf-8") as f:
            f.write(content)

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

        async def _read_messages():
            async with lock:
                try:
                    # Run file I/O in thread pool to avoid blocking
                    lines = await asyncio.to_thread(self._read_file_lines, msg_file)
                except Exception:
                    return []
            return lines

        try:
            lines = await self._run_with_timeout(
                _read_messages(),
                timeout=DEFAULT_DB_TIMEOUT,
                operation="get_recent_messages",
            )
        except DatabaseError:
            log.warning("Timeout reading messages for chat %s", chat_id)
            return []

        # Get last N lines, then parse with checksum validation
        recent_lines = lines[-limit:] if len(lines) > limit else lines
        messages = []
        corruption_detected = False

        for line_num_offset, line in enumerate(recent_lines):
            msg = safe_json_parse_line(line, default=None, log_errors=False)
            if msg is None:
                if line.strip():  # Only log if line wasn't empty
                    actual_line_num = (
                        len(lines) - len(recent_lines) + line_num_offset + 1
                    )
                    log.warning(
                        "JSON corruption detected in chat %s line %d",
                        chat_id,
                        actual_line_num,
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
                )
                corruption_detected = True
                # Skip corrupted message - recovery mechanism
                continue

            messages.append(
                {
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "name": msg.get("name"),
                }
            )

        # Log summary if corruption was detected
        if corruption_detected:
            log.warning(
                "Corruption detected while reading messages for chat %s. "
                "Some messages may have been skipped. Consider running repair_message_file().",
                chat_id,
            )

        # Already in chronological order (oldest first) since we read from file
        return messages

    def _read_file_lines(self, file_path: Path) -> List[str]:
        """Synchronous helper to read file lines using deque for memory efficiency."""
        with file_path.open("r", encoding="utf-8") as f:
            return list(deque(f, maxlen=MAX_MESSAGE_HISTORY))

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
                # Only flush to disk if enough time has passed since last save
                if (now - self._last_chats_save) >= self._chats_save_interval:
                    await self._save_chats()
                    self._last_chats_save = now
                    self._chats_dirty = False

        await self._run_with_timeout(
            _upsert(),
            timeout=DEFAULT_DB_TIMEOUT,
            operation="upsert_chat",
        )

    async def flush_chats(self) -> None:
        """Force-flush dirty chat metadata to disk."""
        async with self._chats_lock:
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
        async with self._chats_lock:
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
