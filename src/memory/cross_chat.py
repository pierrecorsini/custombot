"""
cross_chat.py — Opt-in cross-chat memory sharing with privacy controls.

Users explicitly tag memories as shared. Shared memories are stored in a
central JSONL file with scoping (all_chats or specific chat_ids).
Shared memories are read-only outside their origin chat.

Storage: workspace/.data/shared_memories.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.utils.json_utils import json_dumps

log = logging.getLogger(__name__)

SHARED_MEMORIES_FILE = ".data/shared_memories.jsonl"
ACCESS_LOG_FILE = ".data/shared_memories_access.jsonl"


@dataclass(slots=True)
class SharedMemory:
    """A memory shared across chats."""

    id: str
    origin_chat_id: str
    content: str
    scope: str  # "all_chats" | comma-separated chat_ids
    timestamp: float
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "origin_chat_id": self.origin_chat_id,
            "content": self.content,
            "scope": self.scope,
            "timestamp": self.timestamp,
            "importance": self.importance,
            "tags": self.tags,
        }

    def is_visible_to(self, chat_id: str) -> bool:
        if self.scope == "all_chats":
            return True
        return chat_id in self.scope.split(",")


class CrossChatMemory:
    """Opt-in cross-chat memory sharing with access logging."""

    def __init__(self, workspace_root: str) -> None:
        self._root = Path(workspace_root)
        self._shared_path = self._root / SHARED_MEMORIES_FILE
        self._access_path = self._root / ACCESS_LOG_FILE

    # ── sharing ──────────────────────────────────────────────────────────

    async def share_memory(
        self,
        memory_id: str,
        origin_chat_id: str,
        content: str,
        scope: str,
        importance: float = 0.5,
        tags: list[str] | None = None,
    ) -> SharedMemory:
        """Tag a memory as shared with the given scope."""
        self._root.mkdir(parents=True, exist_ok=True)
        shared = SharedMemory(
            id=memory_id,
            origin_chat_id=origin_chat_id,
            content=content,
            scope=scope,
            timestamp=time.time(),
            importance=importance,
            tags=tags or [],
        )
        line = json_dumps(shared.to_dict()) + "\n"
        await asyncio.to_thread(self._append_line, self._shared_path, line)
        log.info("Shared memory %s with scope=%s", memory_id, scope)
        return shared

    async def get_shared_memories(
        self,
        chat_id: str,
        limit: int = 50,
    ) -> list[SharedMemory]:
        """Return shared memories visible to *chat_id* (read-only)."""
        if not self._shared_path.exists():
            return []
        raw = await asyncio.to_thread(self._shared_path.read_text, encoding="utf-8")
        results: list[SharedMemory] = []
        for line in reversed(raw.splitlines()):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            shared = self._from_dict(data)
            if shared.is_visible_to(chat_id):
                results.append(shared)
            if len(results) >= limit:
                break
        await self._log_access(chat_id, len(results))
        return results

    async def revoke_sharing(self, memory_id: str) -> bool:
        """Remove a shared memory by id. Returns True if found and removed."""
        if not self._shared_path.exists():
            return False
        raw = await asyncio.to_thread(self._shared_path.read_text, encoding="utf-8")
        kept_lines: list[str] = []
        found = False
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                kept_lines.append(line)
                continue
            if data.get("id") == memory_id:
                found = True
            else:
                kept_lines.append(line)
        if found:
            content = "".join(line + "\n" for line in kept_lines)
            await asyncio.to_thread(self._shared_path.write_text, content, encoding="utf-8")
            log.info("Revoked sharing for memory %s", memory_id)
        return found

    # ── internal ─────────────────────────────────────────────────────────

    @staticmethod
    def _from_dict(data: dict[str, Any]) -> SharedMemory:
        return SharedMemory(
            id=data["id"],
            origin_chat_id=data["origin_chat_id"],
            content=data["content"],
            scope=data["scope"],
            timestamp=data.get("timestamp", 0.0),
            importance=data.get("importance", 0.5),
            tags=data.get("tags", []),
        )

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    async def _log_access(self, chat_id: str, count: int) -> None:
        """Append an access audit entry."""
        self._root.mkdir(parents=True, exist_ok=True)
        entry = json_dumps({
            "chat_id": chat_id,
            "count": count,
            "timestamp": time.time(),
        }) + "\n"
        await asyncio.to_thread(self._append_line, self._access_path, entry)
