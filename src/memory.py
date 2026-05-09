"""
memory.py — Per-chat persistent memory.

Inspired by picoclaw's workspace/memory/ layout and NanoClaw's per-group
CLAUDE.md isolation.

Each chat gets its own file:
  <workspace>/<chat_id>/MEMORY.md

The LLM reads this at the start of every turn (via the system message).
The `remember` skill can update it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

import time as _time
from datetime import timezone

from src.constants import MAX_LRU_CACHE_SIZE, MTIME_CACHE_MISSING_TTL
from src.core.errors import NonCriticalCategory, log_noncritical
from src.security import PathSecurityError, is_path_in_workspace
from src.utils import LRUDict
from src.utils.path import sanitize_path_component

log = logging.getLogger(__name__)

MEMORY_FILENAME = "MEMORY.md"
AGENTS_FILENAME = "AGENTS.md"
RECOVERY_LOG_FILENAME = "RECOVERY.md"
ORIGIN_ID_FILENAME = ".chat_id"

# SHA256 checksum truncated to this many hex characters (128 bits)
CHECKSUM_LENGTH = 32
BACKUP_DIR = "backups"


@dataclass(slots=True)
class MemoryCorruptionResult:
    """Result of memory file corruption detection."""

    file_path: str
    is_corrupted: bool
    checksum_valid: bool
    error_details: List[str] = field(default_factory=list)
    backup_path: Optional[str] = None
    repaired: bool = False


_DEFAULT_AGENTS_MD = """\
# Agent Instructions

You are a helpful AI assistant. Be concise, accurate, and friendly.

## Guidelines

- Always explain what you're doing before taking actions
- Ask for clarification when a request is ambiguous
- Use tools to help accomplish tasks
- Be proactive and helpful
- Learn from user feedback

## Skills

You can discover and install new skills using:
- `skills_find <query>` — Search for skills
- `skills_add <package>` — Install a skill
- `skills_list` — List installed skills
- `skills_remove <name>` — Remove a skill
"""

# Public alias so callers (e.g. ContextAssembler) can substitute the
# canonical default when AGENTS.md hasn't been seeded yet.
DEFAULT_AGENTS_MD: str = _DEFAULT_AGENTS_MD


# ── module-level helpers (shared by MtimeCache) ────────────────────────────


def _stat_and_read(path: Path, cached_mtime: Optional[float]) -> tuple:
    """Stat + conditionally read a file in a single thread hop.

    Returns ``(None, None)`` if the file does not exist, keeping the
    existence check off the event loop.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None, None
    mtime = st.st_mtime
    if cached_mtime is not None and mtime == cached_mtime:
        return mtime, None
    return mtime, path.read_text(encoding="utf-8")


def _track_cache_event(hit: bool) -> None:
    """Report a cache hit or miss to the performance metrics collector."""
    try:
        from src.monitoring.performance import get_metrics_collector

        if hit:
            get_metrics_collector().track_memory_cache_hit()
        else:
            get_metrics_collector().track_memory_cache_miss()
    except Exception:
        log_noncritical(
            NonCriticalCategory.CACHE_TRACKING,
            "Failed to track memory cache %s event",
            "hit" if hit else "miss",
            logger=log,
            exc_info=True,
        )


# ── MtimeCache ─────────────────────────────────────────────────────────────


class MtimeCache:
    """mtime-based file content cache with automatic hit/miss tracking.

    Encapsulates the ``(mtime, content)`` tuple and LRU eviction so
    callers don't repeat the stat → compare → read → store dance for
    every file they want to cache.

    When a file doesn't exist, the key is recorded in ``_missing`` (a
    bounded ``LRUDict``) with a timestamp.  Subsequent reads within
    ``MTIME_CACHE_MISSING_TTL`` seconds return ``None`` immediately,
    avoiding the ``asyncio.to_thread()`` hop that would otherwise stat
    the filesystem only to discover the file is still absent.

    ``_missing`` is capped at ``max_size`` entries (matching ``_cache``)
    with FIFO eviction, preventing unbounded growth when many unique chat
    IDs probe for nonexistent files.
    """

    __slots__ = ("_cache", "_missing", "_hits", "_misses")

    def __init__(self, max_size: int = MAX_LRU_CACHE_SIZE) -> None:
        self._cache: LRUDict = LRUDict(max_size=max_size)
        # {key: monotonic timestamp} — tracks keys whose file was absent.
        # Bounded by the same max_size as _cache to prevent unbounded growth
        # when many unique chat IDs probe for nonexistent files.
        self._missing: LRUDict = LRUDict(max_size=max_size)
        self._hits: int = 0
        self._misses: int = 0

    async def read(self, key: str, path: Path) -> Optional[str]:
        """Read *path* with mtime-based caching under *key*.

        Returns the file content (from cache on hit, from disk on miss),
        or ``None`` if the file does not exist.
        Raises :class:`OSError` on I/O failures.
        """
        # Fast path: file was previously absent and TTL hasn't expired
        missing_ts = self._missing.get(key)
        if missing_ts is not None:
            if _time.monotonic() - missing_ts < MTIME_CACHE_MISSING_TTL:
                return None
            # TTL expired — allow a fresh stat to detect file creation
            self._missing.pop(key, None)

        cached = self._cache.get(key)
        cached_mtime = cached[0] if cached else None
        try:
            mtime, content = await asyncio.to_thread(
                _stat_and_read,
                path,
                cached_mtime,
            )
        except OSError as exc:
            raise OSError(f"Read failed for {path}: {exc}") from exc
        if mtime is None:
            # File does not exist — remember as missing
            self._missing[key] = _time.monotonic()
            return None
        # File exists — clear any stale missing marker
        self._missing.pop(key, None)
        if content is None:
            # Cache hit — mtime unchanged, reuse cached content
            self._hits += 1
            _track_cache_event(hit=True)
            return cached[1]
        # Cache miss — file changed or not yet cached
        self._misses += 1
        _track_cache_event(hit=False)
        self._cache[key] = (mtime, content)
        return content

    def invalidate(self, key: str) -> None:
        """Remove *key* from the cache (e.g. after a write)."""
        self._cache.pop(key, None)
        self._missing.pop(key, None)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return key in self._cache


# ── Memory ─────────────────────────────────────────────────────────────────


class Memory:
    """File-based per-chat memory manager."""

    def __init__(self, workspace_root: str) -> None:
        self._root = Path(workspace_root)
        self._memory_cache = MtimeCache(max_size=MAX_LRU_CACHE_SIZE)
        self._agents_cache = MtimeCache(max_size=MAX_LRU_CACHE_SIZE)
        self._path_cache: LRUDict = LRUDict(max_size=MAX_LRU_CACHE_SIZE)
        # Bounded set of chat_ids whose directories are known to exist on disk.
        # Avoids repeated mkdir(parents=True, exist_ok=True) syscalls on the
        # hot write path.
        self._known_dirs: LRUDict = LRUDict(max_size=MAX_LRU_CACHE_SIZE)

    # ── internal ───────────────────────────────────────────────────────────

    def _resolve_chat_path(self, chat_id: str) -> Path:
        """Build and validate the chat directory path (cached)."""
        cached = self._path_cache.get(chat_id)
        if cached is not None:
            return cached
        d = self._root / "whatsapp_data" / sanitize_path_component(chat_id)
        self._validate_path(d, chat_id)
        self._path_cache[chat_id] = d
        return d

    def _chat_dir(self, chat_id: str) -> Path:
        """Return the chat directory path without creating it."""
        return self._resolve_chat_path(chat_id)

    def _ensure_chat_dir(self, chat_id: str) -> Path:
        """Return the chat directory path, creating it if needed.

        Caches known-existing directories to skip the ``mkdir`` syscall
        on subsequent calls for the same *chat_id*.
        """
        if self._known_dirs.get(chat_id) is not None:
            return self._resolve_chat_path(chat_id)
        d = self._resolve_chat_path(chat_id)
        d.mkdir(parents=True, exist_ok=True)
        self._known_dirs[chat_id] = True
        return d

    def _validate_path(self, path: Path, chat_id: str) -> None:
        """Ensure resolved path stays within the workspace root."""
        workspace_data = self._root / "whatsapp_data"
        if not is_path_in_workspace(workspace_data, path.resolve()):
            log.warning("Path traversal blocked for chat_id=%s", chat_id)
            raise PathSecurityError(
                f"Workspace escape blocked for chat_id={chat_id!r}",
                path=str(path),
                reason="path_traversal",
            )

    # ── public API ─────────────────────────────────────────────────────────

    ORIGIN_ID_FILENAME = ".chat_id"

    def ensure_workspace(self, chat_id: str) -> Path:
        """
        Create the per-chat workspace directory and seed initial files
        (AGENTS.md, .chat_id) if they don't exist yet.
        Returns the workspace Path.
        """
        # Invalidate path cache so ensure_workspace forces a fresh resolve
        # with full validation before creating directories on disk.
        self._path_cache.pop(chat_id, None)
        self._known_dirs.pop(chat_id, None)
        d = self._ensure_chat_dir(chat_id)
        if self._atomic_seed(d / AGENTS_FILENAME, _DEFAULT_AGENTS_MD):
            self._agents_cache.invalidate(chat_id)
            log.debug("Seeded %s", d / AGENTS_FILENAME)
        # Store original chat_id for reverse lookup (JID reconstruction)
        self._atomic_seed(d / self.ORIGIN_ID_FILENAME, chat_id)
        return d

    @staticmethod
    def _atomic_seed(path: Path, content: str) -> bool:
        """Atomically create *path* with *content* if it doesn't exist.

        Uses ``os.O_EXCL | os.O_CREAT`` on a temp file then renames,
        eliminating the TOCTOU window of an ``exists()`` check.

        Returns ``True`` if the file was created, ``False`` if it already
        existed.
        """
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            # Another coroutine is already writing — it will finish.
            return False
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        try:
            tmp.rename(path)
        except FileExistsError:
            # Final file appeared (another writer finished first) — clean up.
            tmp.unlink(missing_ok=True)
            return False
        return True

    async def read_memory(self, chat_id: str) -> Optional[str]:
        """Return the contents of MEMORY.md, or None if it doesn't exist."""
        content = await self._memory_cache.read(
            chat_id,
            self._chat_dir(chat_id) / MEMORY_FILENAME,
        )
        if content is None:
            return None
        return content.strip() or None

    async def write_memory(self, chat_id: str, content: str) -> None:
        """Overwrite MEMORY.md with new content."""
        path = self._ensure_chat_dir(chat_id) / MEMORY_FILENAME
        await asyncio.to_thread(path.write_text, content.strip() + "\n", encoding="utf-8")
        self._memory_cache.invalidate(chat_id)
        log.debug("Memory updated for chat %s", chat_id)

    # ── corruption detection ────────────────────────────────────────────────

    def _calculate_checksum(self, content: str) -> str:
        """
        Calculate SHA256 checksum for memory content.

        Args:
            content: Memory content string

        Returns:
            Hexadecimal checksum string (first 32 chars = 128 bits)
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:CHECKSUM_LENGTH]

    def _get_checksum_file(self, chat_id: str) -> Path:
        """Get the checksum file path for a chat's memory."""
        return self._chat_dir(chat_id) / ".memory_checksum"

    def detect_memory_corruption(self, chat_id: str) -> MemoryCorruptionResult:
        """
        Detect corruption in a chat's MEMORY.md file.

        Validates file integrity by comparing stored checksum with calculated checksum.

        Args:
            chat_id: Chat/group ID

        Returns:
            MemoryCorruptionResult with details about any corruption found
        """
        path = self._chat_dir(chat_id) / MEMORY_FILENAME
        result = MemoryCorruptionResult(
            file_path=str(path), is_corrupted=False, checksum_valid=True
        )

        if not path.exists():
            return result

        try:
            content = path.read_text(encoding="utf-8")

            # Check if checksum file exists
            checksum_path = self._get_checksum_file(chat_id)
            if checksum_path.exists():
                stored_checksum = checksum_path.read_text(encoding="utf-8").strip()
                calculated_checksum = self._calculate_checksum(content)

                if stored_checksum != calculated_checksum:
                    result.is_corrupted = True
                    result.checksum_valid = False
                    result.error_details.append(
                        f"Checksum mismatch: expected {stored_checksum}, got {calculated_checksum}"
                    )
                    log.warning(
                        "Memory corruption detected for chat %s: checksum mismatch",
                        chat_id,
                    )

        except OSError as exc:
            result.is_corrupted = True
            result.error_details.append(f"Failed to read memory file: {exc}")
            log.error("Failed to read memory file for chat %s: %s", chat_id, exc)

        return result

    def _backup_memory_file_sync(self, chat_id: str) -> Optional[str]:
        """Create a backup of a chat's MEMORY.md file (shared implementation).

        Both the sync and async backup paths delegate here so the logic
        is defined once.
        """
        path = self._chat_dir(chat_id) / MEMORY_FILENAME

        if not path.exists():
            return None

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_dir = self._root / BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)

        safe_id = sanitize_path_component(chat_id)
        backup_file = backup_dir / f"{safe_id}_{timestamp}.md.bak"

        try:
            shutil.copy2(path, backup_file)
            log.info("Created memory backup: %s", backup_file)
            return str(backup_file)
        except OSError as exc:
            log.error("Failed to create memory backup: %s", exc)
            return None

    async def abackup_memory_file(self, chat_id: str) -> Optional[str]:
        """Async counterpart — offloads backup I/O to a thread."""
        return await asyncio.to_thread(self._backup_memory_file_sync, chat_id)

    def _repair_memory_file_sync(self, chat_id: str, backup: bool = True) -> MemoryCorruptionResult:
        """
        Attempt to repair a corrupted MEMORY.md file.

        Creates a backup and clears the corrupted content.

        Args:
            chat_id: Chat/group ID
            backup: Whether to create backup before repair

        Returns:
            MemoryCorruptionResult with repair details
        """
        result = self.detect_memory_corruption(chat_id)

        if not result.is_corrupted:
            return result

        # Create backup if requested
        if backup:
            result.backup_path = self._backup_memory_file_sync(chat_id)

        # For memory files, we clear the corrupted content
        # The LLM will regenerate it on next interaction
        path = self._ensure_chat_dir(chat_id) / MEMORY_FILENAME

        try:
            # Clear the file by writing empty content
            path.write_text("", encoding="utf-8")

            # Remove the invalid checksum
            checksum_path = self._get_checksum_file(chat_id)
            if checksum_path.exists():
                checksum_path.unlink()

            result.repaired = True
            log.info("Repaired memory file for chat %s", chat_id)

        except OSError as exc:
            result.error_details.append(f"Failed to repair: {exc}")
            log.error("Failed to repair memory file for chat %s: %s", chat_id, exc)

        return result

    async def arepair_memory_file(
        self,
        chat_id: str,
        backup: bool = True,
    ) -> MemoryCorruptionResult:
        """Async counterpart of _repair_memory_file_sync — offloads I/O to a thread."""
        result = await asyncio.to_thread(self.detect_memory_corruption, chat_id)

        if not result.is_corrupted:
            return result

        if backup:
            result.backup_path = await self.abackup_memory_file(chat_id)

        path = self._ensure_chat_dir(chat_id) / MEMORY_FILENAME

        try:
            await asyncio.to_thread(path.write_text, "", encoding="utf-8")

            checksum_path = self._get_checksum_file(chat_id)
            if checksum_path.exists():
                await asyncio.to_thread(checksum_path.unlink)

            result.repaired = True
            log.info("Repaired memory file for chat %s", chat_id)
        except OSError as exc:
            result.error_details.append(f"Failed to repair: {exc}")
            log.error("Failed to repair memory file for chat %s: %s", chat_id, exc)

        return result

    async def read_memory_with_validation(self, chat_id: str) -> Optional[str]:
        """
        Return the contents of MEMORY.md with corruption detection.

        Logs corruption events and returns None if corruption is detected.

        Args:
            chat_id: Chat/group ID

        Returns:
            Memory content or None if corrupted/missing
        """
        corruption = await asyncio.to_thread(self.detect_memory_corruption, chat_id)

        if corruption.is_corrupted:
            log.warning(
                "Corruption detected in memory for chat %s: %s",
                chat_id,
                corruption.error_details,
            )
            return None

        return await self.read_memory(chat_id)

    async def write_memory_with_checksum(self, chat_id: str, content: str) -> None:
        """
        Write MEMORY.md with checksum for corruption detection.

        Args:
            chat_id: Chat/group ID
            content: Memory content to write
        """
        path = self._ensure_chat_dir(chat_id) / MEMORY_FILENAME
        stripped_content = content.strip()
        file_content = stripped_content + "\n"

        await asyncio.to_thread(path.write_text, file_content, encoding="utf-8")

        # Write checksum of the actual file content
        checksum = self._calculate_checksum(file_content)
        checksum_path = self._get_checksum_file(chat_id)
        await asyncio.to_thread(checksum_path.write_text, checksum, encoding="utf-8")

        self._memory_cache.invalidate(chat_id)
        log.debug("Memory updated with checksum for chat %s", chat_id)

    async def read_agents_md(self, chat_id: str) -> str:
        """Return AGENTS.md content (system persona / extra instructions)."""
        content = await self._agents_cache.read(
            chat_id,
            self._chat_dir(chat_id) / AGENTS_FILENAME,
        )
        if content is None:
            raise FileNotFoundError(
                f"AGENTS.md not found for chat {chat_id}. "
                f"Run ensure_workspace() first or check workspace integrity."
            )
        return content

    def workspace_path(self, chat_id: str) -> Path:
        """Return the isolated sandbox Path for this chat."""
        return self._chat_dir(chat_id)

    @property
    def cache_hits(self) -> int:
        """Total number of cache hits across memory and agents caches."""
        return self._memory_cache.hits + self._agents_cache.hits

    @property
    def cache_misses(self) -> int:
        """Total number of cache misses across memory and agents caches."""
        return self._memory_cache.misses + self._agents_cache.misses

    async def log_recovery_event(
        self,
        chat_id: str,
        preserved_count: int,
        rebuilt_count: int,
        total_count: int,
        errors: Optional[list] = None,
    ) -> None:
        """
        Log a recovery event to the chat's workspace for user notification.

        Creates or appends to RECOVERY.md with details about the index recovery.
        File I/O is offloaded to a thread to avoid blocking the event loop.

        Args:
            chat_id: The chat ID where recovery occurred
            preserved_count: Number of entries preserved from corrupted index
            rebuilt_count: Number of entries rebuilt from message files
            total_count: Total entries after recovery
            errors: Optional list of error messages
        """
        d = self._ensure_chat_dir(chat_id)
        recovery_path = d / RECOVERY_LOG_FILENAME

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        entry_lines = [
            f"## Recovery Event - {timestamp}",
            "",
            f"- **Preserved entries**: {preserved_count}",
            f"- **Rebuilt from files**: {rebuilt_count}",
            f"- **Total recovered**: {total_count}",
        ]

        if errors:
            entry_lines.append("- **Errors**:")
            for err in errors[:5]:  # Limit to first 5 errors
                entry_lines.append(f"  - {err}")

        entry_lines.append("")
        entry = "\n".join(entry_lines)

        await asyncio.to_thread(self._write_recovery_log_sync, recovery_path, entry)

        log.info(
            "Logged recovery event for chat %s: %d total entries recovered",
            chat_id,
            total_count,
        )

    @staticmethod
    def _write_recovery_log_sync(recovery_path: Path, entry: str) -> None:
        """Synchronous file I/O for recovery log — run via asyncio.to_thread()."""
        if recovery_path.exists():
            existing = recovery_path.read_text(encoding="utf-8")
            recovery_path.write_text(existing + "\n" + entry, encoding="utf-8")
        else:
            header = "# Message Index Recovery Log\n\n"
            recovery_path.write_text(header + entry, encoding="utf-8")

    def has_recovery_events(self, chat_id: str) -> bool:
        """
        Check if there are any recovery events logged for this chat.

        Args:
            chat_id: The chat ID to check

        Returns:
            True if RECOVERY.md exists, False otherwise
        """
        d = self._chat_dir(chat_id)
        return (d / RECOVERY_LOG_FILENAME).exists()

    def clear_recovery_log(self, chat_id: str) -> None:
        """
        Remove the recovery log file for a chat.

        Args:
            chat_id: The chat ID to clear recovery log for
        """
        d = self._ensure_chat_dir(chat_id)
        recovery_path = d / RECOVERY_LOG_FILENAME
        if recovery_path.exists():
            recovery_path.unlink()
            log.debug("Cleared recovery log for chat %s", chat_id)


# ── backward compatibility ──────────────────────────────────────────────────

# Alias so external modules that imported ``_safe_name`` from this file
# keep working without changes.
_safe_name = sanitize_path_component
