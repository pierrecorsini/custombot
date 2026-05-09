"""
src/db/read_model.py — Read-optimized in-memory index for conversation retrieval (CQRS-lite).

Write path: JSONL append (write-optimized, already handled by MessageStore).
Read path: in-memory index rebuilt on startup, updated on each write.

Provides fast lookups for:
  - Recent messages (from in-memory buffer, no disk I/O)
  - Keyword search (via inverted index)

The index is rebuilt from JSONL files on startup and kept in sync
by hooking into the write path.
"""

from __future__ import annotations

import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.utils import safe_json_parse, JsonParseMode

log = logging.getLogger(__name__)

# Per-chat message buffer cap (LRU eviction beyond this).
_MAX_MESSAGES_PER_CHAT = 500
# Maximum chats retained in the index.
_MAX_INDEXED_CHATS = 10_000
# Minimum token length for keyword indexing.
_MIN_KEYWORD_LENGTH = 3


@dataclass(slots=True)
class ChatIndex:
    """Per-chat in-memory index entry."""

    message_count: int = 0
    last_message_time: float = 0.0
    # Ring buffer of recent messages (oldest first).
    messages: list[dict[str, Any]] = field(default_factory=list)
    # Inverted index: keyword → set of message indices in ``messages``.
    keyword_index: dict[str, set[int]] = field(default_factory=dict)


def _tokenize(text: str) -> set[str]:
    """Extract searchable keywords from text."""
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return {w for w in words if len(w) >= _MIN_KEYWORD_LENGTH}


class ConversationReadModel:
    """In-memory read model for fast conversation retrieval.

    Rebuilds index from JSONL on startup, then updates on each write.
    Falls back to full JSONL scan for queries that miss the index.
    """

    def __init__(self, messages_dir: Path, max_per_chat: int = _MAX_MESSAGES_PER_CHAT) -> None:
        self._messages_dir = messages_dir
        self._max_per_chat = max_per_chat
        self._chats: OrderedDict[str, ChatIndex] = OrderedDict()

    # ── startup rebuild ───────────────────────────────────────────────────

    def rebuild_from_jsonl(self) -> int:
        """Rebuild the in-memory index from all JSONL files.

        Returns the number of chats indexed.
        """
        self._chats.clear()
        if not self._messages_dir.exists():
            return 0

        count = 0
        for jsonl_file in self._messages_dir.glob("*.jsonl"):
            chat_id = jsonl_file.stem
            self._index_file(chat_id, jsonl_file)
            count += 1

        log.info("ReadModel rebuilt: %d chats indexed", count)
        return count

    def _index_file(self, chat_id: str, path: Path) -> None:
        """Parse a single JSONL file into the index."""
        idx = ChatIndex()
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            msg = safe_json_parse(line, default=None, mode=JsonParseMode.LINE)
            if msg is None or msg.get("type") == "header":
                continue

            idx.message_count += 1
            ts = msg.get("timestamp", 0)
            if ts > idx.last_message_time:
                idx.last_message_time = ts

            # Buffer recent messages (keep the last N).
            record = {
                "role": msg.get("role", "user"),
                "content": msg.get("content", ""),
                "timestamp": ts,
            }
            idx.messages.append(record)
            if len(idx.messages) > self._max_per_chat:
                idx.messages.pop(0)

        # Build keyword index from buffered messages.
        for i, msg in enumerate(idx.messages):
            keywords = _tokenize(msg.get("content", ""))
            for kw in keywords:
                if kw not in idx.keyword_index:
                    idx.keyword_index[kw] = set()
                idx.keyword_index[kw].add(i)

        self._chats[chat_id] = idx
        self._chats.move_to_end(chat_id)

    # ── write-side updates ────────────────────────────────────────────────

    def on_message_written(self, chat_id: str, message: dict[str, Any]) -> None:
        """Update index after a message is written to JSONL."""
        idx = self._chats.get(chat_id)
        if idx is None:
            idx = ChatIndex()
            self._chats[chat_id] = idx
            self._evict_if_full()

        self._chats.move_to_end(chat_id)

        idx.message_count += 1
        ts = message.get("timestamp", time.time())
        if ts > idx.last_message_time:
            idx.last_message_time = ts

        record = {
            "role": message.get("role", "user"),
            "content": message.get("content", ""),
            "timestamp": ts,
        }
        idx.messages.append(record)

        # Update keyword index for the new message.
        pos = len(idx.messages) - 1
        keywords = _tokenize(record["content"])
        for kw in keywords:
            if kw not in idx.keyword_index:
                idx.keyword_index[kw] = set()
            idx.keyword_index[kw].add(pos)

        # Evict oldest messages if over cap.
        if len(idx.messages) > self._max_per_chat:
            self._evict_oldest(idx)

    def _evict_oldest(self, idx: ChatIndex) -> None:
        """Remove the oldest message and clean its keyword entries."""
        # Shift all keyword index positions down by 1 and remove references to pos 0.
        new_kw: dict[str, set[int]] = {}
        for kw, positions in idx.keyword_index.items():
            shifted = {p - 1 for p in positions if p > 0}
            if shifted:
                new_kw[kw] = shifted
        idx.keyword_index = new_kw
        idx.messages.pop(0)

    def _evict_if_full(self) -> None:
        if len(self._chats) <= _MAX_INDEXED_CHATS:
            return
        evicted_id, _ = self._chats.popitem(last=False)
        log.debug("ReadModel evicted chat %s (LRU cap)", evicted_id)

    # ── read-side queries ─────────────────────────────────────────────────

    def get_recent_fast(self, chat_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return recent messages from the in-memory index.

        Falls back to an empty list if the chat is not indexed.
        The caller should fall back to JSONL scan if needed.
        """
        idx = self._chats.get(chat_id)
        if idx is None:
            return []
        return idx.messages[-limit:]

    def search_fast(self, chat_id: str, query: str) -> list[dict[str, Any]]:
        """Keyword search via the inverted index.

        Returns messages whose content contains any query keyword.
        Falls back to empty list if chat is not indexed.
        """
        idx = self._chats.get(chat_id)
        if idx is None:
            return []

        query_keywords = _tokenize(query)
        if not query_keywords:
            return []

        # Union of all positions matching any keyword.
        matching_positions: set[int] = set()
        for kw in query_keywords:
            positions = idx.keyword_index.get(kw)
            if positions:
                matching_positions.update(positions)

        if not matching_positions:
            return []

        # Return matching messages, ordered by position (chronological).
        results = []
        for pos in sorted(matching_positions):
            if 0 <= pos < len(idx.messages):
                results.append(idx.messages[pos])
        return results

    # ── stats ─────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        """Return index statistics for monitoring."""
        total_messages = sum(idx.message_count for idx in self._chats.values())
        return {
            "indexed_chats": len(self._chats),
            "total_messages": total_messages,
            "max_per_chat": self._max_per_chat,
        }
