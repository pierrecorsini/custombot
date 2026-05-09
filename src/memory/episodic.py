"""
episodic.py — Structured episodic memory for significant conversation events.

Stores decisions, facts, and user preferences extracted from conversations
as typed episodes in JSONL files. Supports keyword-based extraction and
optional vector search for semantic retrieval.

Storage: workspace/.data/episodic/<chat_id>.jsonl
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from src.utils.json_utils import json_dumps
from src.utils.path import sanitize_path_component

log = logging.getLogger(__name__)

EPISODIC_DIR = ".data/episodic"

# Keyword patterns for automatic episode extraction.
_DECISION_KEYWORDS = ("i'll ", "let's ", "i want", "we should", "i decide")
_FACT_KEYWORDS = ("my name is", "i live in", "i work at", "i am from", "my birthday")
_PREFERENCE_KEYWORDS = ("i prefer", "i like", "i hate", "i love", "i enjoy", "i dislike")

EPISODE_TYPES = ("decision", "fact", "preference")


@dataclass(slots=True)
class Episode:
    """A single significant conversation episode."""

    id: str
    chat_id: str
    timestamp: float
    type: str  # "decision" | "fact" | "preference"
    content: str
    importance: float = 0.5
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chat_id": self.chat_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "content": self.content,
            "importance": self.importance,
            "tags": self.tags,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Episode:
        return cls(
            id=data["id"],
            chat_id=data["chat_id"],
            timestamp=data["timestamp"],
            type=data["type"],
            content=data["content"],
            importance=data.get("importance", 0.5),
            tags=data.get("tags", []),
        )


class EpisodicMemory:
    """File-based episodic memory with keyword extraction and optional vector search."""

    def __init__(self, workspace_root: str, vector_memory: Any = None) -> None:
        self._root = Path(workspace_root)
        self._episodic_dir = self._root / EPISODIC_DIR
        self._vector_memory = vector_memory

    def _episode_path(self, chat_id: str) -> Path:
        return self._episodic_dir / f"{sanitize_path_component(chat_id)}.jsonl"

    def set_vector_memory(self, vm: Any) -> None:
        self._vector_memory = vm

    # ── storage ──────────────────────────────────────────────────────────

    async def store_episode(self, episode: Episode) -> str:
        """Persist an episode. Returns the episode id."""
        self._episodic_dir.mkdir(parents=True, exist_ok=True)
        path = self._episode_path(episode.chat_id)
        line = json_dumps(episode.to_dict()) + "\n"
        await asyncio.to_thread(self._append_line, path, line)

        if self._vector_memory is not None:
            try:
                await self._vector_memory.save(
                    episode.chat_id, episode.content, f"episodic:{episode.type}"
                )
            except Exception:
                log.debug("Vector save failed for episode %s", episode.id, exc_info=True)

        log.debug("Stored episode %s for chat %s", episode.id, episode.chat_id)
        return episode.id

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    # ── retrieval ────────────────────────────────────────────────────────

    async def get_episodes(
        self,
        chat_id: str,
        episode_type: Optional[str] = None,
        limit: int = 50,
    ) -> list[Episode]:
        """Return episodes for a chat, optionally filtered by type."""
        path = self._episode_path(chat_id)
        if not path.exists():
            return []
        raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
        episodes: list[Episode] = []
        for line in reversed(raw.splitlines()):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            ep = Episode.from_dict(data)
            if episode_type and ep.type != episode_type:
                continue
            episodes.append(ep)
            if len(episodes) >= limit:
                break
        return episodes

    # ── search ───────────────────────────────────────────────────────────

    async def search_episodes(self, query: str, limit: int = 10) -> list[Episode]:
        """Search episodes via vector memory (semantic) or keyword fallback."""
        if self._vector_memory is not None:
            return await self._vector_search(query, limit)
        return await self._keyword_search(query, limit)

    async def _vector_search(self, query: str, limit: int) -> list[Episode]:
        results = await self._vector_memory.search("", query, limit=limit)
        episodes: list[Episode] = []
        for r in results:
            text = r.get("text", "")
            ep = Episode(
                id=str(r.get("id", "")),
                chat_id=r.get("chat_id", ""),
                timestamp=r.get("created_at", 0.0),
                type="",
                content=text,
            )
            episodes.append(ep)
        return episodes[:limit]

    async def _keyword_search(self, query: str, limit: int) -> list[Episode]:
        """Fallback keyword search across all episode files."""
        if not self._episodic_dir.exists():
            return []
        query_lower = query.lower()
        matches: list[Episode] = []
        files = await asyncio.to_thread(lambda: list(self._episodic_dir.glob("*.jsonl")))
        for path in files:
            raw = await asyncio.to_thread(path.read_text, encoding="utf-8")
            for line in reversed(raw.splitlines()):
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if query_lower in data.get("content", "").lower():
                    matches.append(Episode.from_dict(data))
                if len(matches) >= limit:
                    return matches
        return matches

    # ── extraction ───────────────────────────────────────────────────────

    def extract_episodes(self, chat_id: str, text: str) -> list[Episode]:
        """Extract potential episodes from conversation text via keyword matching."""
        text_lower = text.lower()
        episodes: list[Episode] = []
        now = time.time()

        for kw in _DECISION_KEYWORDS:
            if kw in text_lower:
                episodes.append(self._make_episode(chat_id, now, "decision", text))
                break

        for kw in _FACT_KEYWORDS:
            if kw in text_lower:
                episodes.append(self._make_episode(chat_id, now, "fact", text))
                break

        for kw in _PREFERENCE_KEYWORDS:
            if kw in text_lower:
                episodes.append(self._make_episode(chat_id, now, "preference", text))
                break

        return episodes

    @staticmethod
    def _make_episode(chat_id: str, ts: float, ep_type: str, content: str) -> Episode:
        return Episode(
            id=uuid.uuid4().hex[:16],
            chat_id=chat_id,
            timestamp=ts,
            type=ep_type,
            content=content[:500],
            importance=0.5,
        )
