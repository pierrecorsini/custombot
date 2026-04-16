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
import shutil
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from src.constants import MAX_LRU_CACHE_SIZE

log = logging.getLogger(__name__)

MEMORY_FILENAME = "MEMORY.md"
AGENTS_FILENAME = "AGENTS.md"
RECOVERY_LOG_FILENAME = "RECOVERY.md"
BACKUP_DIR = "backups"


@dataclass
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


class Memory:
    """File-based per-chat memory manager."""

    def __init__(self, workspace_root: str) -> None:
        self._root = Path(workspace_root)
        # LRU-bounded caches to prevent unbounded memory growth with many chats
        self._memory_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._agents_cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max_cache_size = MAX_LRU_CACHE_SIZE

    def _cache_get(self, cache: OrderedDict, chat_id: str) -> tuple[float, str] | None:
        """Get from LRU cache, moving key to end (most recently used)."""
        if chat_id in cache:
            cache.move_to_end(chat_id)
            return cache[chat_id]
        return None

    def _cache_put(
        self, cache: OrderedDict, chat_id: str, value: tuple[float, str]
    ) -> None:
        """Put into LRU cache, evicting oldest entry if at capacity."""
        if chat_id in cache:
            cache.move_to_end(chat_id)
            cache[chat_id] = value
        else:
            if len(cache) >= self._max_cache_size:
                cache.popitem(last=False)
            cache[chat_id] = value

    # ── internal ───────────────────────────────────────────────────────────

    def _chat_dir(self, chat_id: str) -> Path:
        """Return the chat directory path without creating it."""
        return self._root / "whatsapp_data" / _safe_name(chat_id)

    def _ensure_chat_dir(self, chat_id: str) -> Path:
        """Return the chat directory path, creating it if needed."""
        d = self._chat_dir(chat_id)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── public API ─────────────────────────────────────────────────────────

    ORIGIN_ID_FILENAME = ".chat_id"

    def ensure_workspace(self, chat_id: str) -> Path:
        """
        Create the per-chat workspace directory and seed initial files
        (AGENTS.md, .chat_id) if they don't exist yet.
        Returns the workspace Path.
        """
        d = self._ensure_chat_dir(chat_id)
        agents_path = d / AGENTS_FILENAME
        if not agents_path.exists():
            agents_path.write_text(_DEFAULT_AGENTS_MD, encoding="utf-8")
            self._agents_cache.pop(chat_id, None)
            log.debug("Seeded %s", agents_path)
        # Store original chat_id for reverse lookup (JID reconstruction)
        origin_path = d / self.ORIGIN_ID_FILENAME
        if not origin_path.exists():
            origin_path.write_text(chat_id, encoding="utf-8")
        return d

    async def read_memory(self, chat_id: str) -> Optional[str]:
        """Return the contents of MEMORY.md, or None if it doesn't exist."""
        path = self._chat_dir(chat_id) / MEMORY_FILENAME
        if not path.exists():
            return None
        # Check mtime-based cache (proper LRU via _cache_get/_cache_put)
        mtime = (await asyncio.to_thread(path.stat)).st_mtime
        cached = self._cache_get(self._memory_cache, chat_id)
        if cached and cached[0] == mtime:
            return cached[1] or None
        content = (await asyncio.to_thread(path.read_text, encoding="utf-8")).strip()
        self._cache_put(self._memory_cache, chat_id, (mtime, content))
        return content or None

    async def write_memory(self, chat_id: str, content: str) -> None:
        """Overwrite MEMORY.md with new content."""
        path = self._ensure_chat_dir(chat_id) / MEMORY_FILENAME
        await asyncio.to_thread(
            path.write_text, content.strip() + "\n", encoding="utf-8"
        )
        self._memory_cache.pop(chat_id, None)
        log.debug("Memory updated for chat %s", chat_id)

    # ── corruption detection ────────────────────────────────────────────────

    def _calculate_checksum(self, content: str) -> str:
        """
        Calculate SHA256 checksum for memory content.

        Args:
            content: Memory content string

        Returns:
            Hexadecimal checksum string (first 16 chars)
        """
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

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

        except Exception as e:
            result.is_corrupted = True
            result.error_details.append(f"Failed to read memory file: {e}")
            log.error("Failed to read memory file for chat %s: %s", chat_id, e)

        return result

    def backup_memory_file(self, chat_id: str) -> Optional[str]:
        """
        Create a backup of a chat's MEMORY.md file.

        Args:
            chat_id: Chat/group ID

        Returns:
            Path to backup file, or None if backup failed
        """
        path = self._chat_dir(chat_id) / MEMORY_FILENAME

        if not path.exists():
            return None

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = self._root / BACKUP_DIR
        backup_dir.mkdir(parents=True, exist_ok=True)

        safe_id = _safe_name(chat_id)
        backup_file = backup_dir / f"{safe_id}_{timestamp}.md.bak"

        try:
            shutil.copy2(path, backup_file)
            log.info("Created memory backup: %s", backup_file)
            return str(backup_file)
        except Exception as e:
            log.error("Failed to create memory backup: %s", e)
            return None

    def repair_memory_file(
        self, chat_id: str, backup: bool = True
    ) -> MemoryCorruptionResult:
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
            result.backup_path = self.backup_memory_file(chat_id)

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

        except Exception as e:
            result.error_details.append(f"Failed to repair: {e}")
            log.error("Failed to repair memory file for chat %s: %s", chat_id, e)

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
        corruption = self.detect_memory_corruption(chat_id)

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

        await asyncio.to_thread(
            path.write_text, stripped_content + "\n", encoding="utf-8"
        )

        # Write checksum
        checksum = self._calculate_checksum(stripped_content)
        checksum_path = self._get_checksum_file(chat_id)
        await asyncio.to_thread(checksum_path.write_text, checksum, encoding="utf-8")

        log.debug("Memory updated with checksum for chat %s", chat_id)

    async def read_agents_md(self, chat_id: str) -> str:
        """Return AGENTS.md content (system persona / extra instructions)."""
        path = self._chat_dir(chat_id) / AGENTS_FILENAME
        if not path.exists():
            raise FileNotFoundError(
                f"AGENTS.md not found for chat {chat_id} at {path}. "
                f"Run ensure_workspace() first or check workspace integrity."
            )
        # Check mtime-based cache (proper LRU via _cache_get/_cache_put)
        mtime = (await asyncio.to_thread(path.stat)).st_mtime
        cached = self._cache_get(self._agents_cache, chat_id)
        if cached and cached[0] == mtime:
            return cached[1]
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        self._cache_put(self._agents_cache, chat_id, (mtime, content))
        return content

    def workspace_path(self, chat_id: str) -> Path:
        """Return the isolated sandbox Path for this chat."""
        return self._chat_dir(chat_id)

    def log_recovery_event(
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

        Args:
            chat_id: The chat ID where recovery occurred
            preserved_count: Number of entries preserved from corrupted index
            rebuilt_count: Number of entries rebuilt from message files
            total_count: Total entries after recovery
            errors: Optional list of error messages
        """
        d = self._ensure_chat_dir(chat_id)
        recovery_path = d / RECOVERY_LOG_FILENAME

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

        # Append to existing file or create new
        if recovery_path.exists():
            existing = recovery_path.read_text(encoding="utf-8")
            recovery_path.write_text(existing + "\n" + entry, encoding="utf-8")
        else:
            header = "# Message Index Recovery Log\n\n"
            recovery_path.write_text(header + entry, encoding="utf-8")

        log.info(
            "Logged recovery event for chat %s: %d total entries recovered",
            chat_id,
            total_count,
        )

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


# ── utilities ──────────────────────────────────────────────────────────────


def _safe_name(chat_id: str) -> str:
    """Strip characters that are unsafe in filesystem paths.

    Uses the same replacement map as db._sanitize_chat_id_for_path()
    to ensure workspace directories and message files use consistent names.
    """
    _SANITIZE_MAP = {
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
    result = chat_id
    for char, replacement in _SANITIZE_MAP.items():
        result = result.replace(char, replacement)
    # Replace any remaining non-alphanumeric characters (except -_. and the replacements above)
    result = "".join(c if c.isalnum() or c in "-_." else "_" for c in result)
    return result
