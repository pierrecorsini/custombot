"""
src/core/project_context.py — Project context loading for LLM context.

Lazily initializes ProjectGraph and ProjectRecall, provides per-chat
project context for the LLM. All SQLite operations run in a thread pool
to avoid blocking the async event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)


class ProjectContextLoader:
    """Loads project context for LLM, lazily initializing graph/recall."""

    def __init__(self, project_store: Any = None) -> None:
        self._store = project_store
        self._graph: Any = None
        self._recall: Any = None

    def _ensure_initialized(self) -> None:
        """Lazily create graph/recall on first access."""
        if self._recall is None and self._store:
            from src.project.graph import ProjectGraph
            from src.project.recall import ProjectRecall

            self._graph = ProjectGraph(self._store)
            self._recall = ProjectRecall(self._store, self._graph)

    @property
    def graph(self) -> Any:
        self._ensure_initialized()
        return self._graph

    @property
    def recall(self) -> Any:
        self._ensure_initialized()
        return self._recall

    async def get(self, chat_id: str) -> str | None:
        """Get concatenated project context for a chat, or None.

        Runs SQLite queries in a thread pool to avoid blocking the event loop.
        """
        if not self._store:
            return None
        try:
            self._ensure_initialized()
            projects = await asyncio.to_thread(self._store.get_chat_projects, chat_id)
            parts = []
            for p in projects:
                ctx = await asyncio.to_thread(self._recall.recall, p["id"])
                if ctx:
                    parts.append(ctx)
            return "\n\n".join(parts) if parts else None
        except Exception as exc:
            log.warning("Failed to load project context for chat %s: %s", chat_id, exc)
            return None
