"""
src/skills/builtin/memory_vss.py — Vector memory skills (sqlite-vec).

Three LLM-callable tools for semantic memory:
  • MemorySaveSkill   — Store a note with auto-generated embedding
  • MemorySearchSkill — Semantic similarity search within chat memories
  • MemoryListSkill   — List recent memories for this chat
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.skills.base import BaseSkill, validate_input
from src.vector_memory import VectorMemory

log = logging.getLogger(__name__)


def _format_ts(epoch: float) -> str:
    """Format a Unix timestamp for display."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ─── Save ────────────────────────────────────────────────────────────────


class MemorySaveSkill(BaseSkill):
    """Save a piece of information to semantic memory."""

    name = "memory_save"
    description = (
        "Save an important piece of information to long-term memory. "
        "The memory is stored with an embedding so it can be recalled "
        "later using semantic search. Use this to remember facts, "
        "preferences, decisions, or any information worth persisting."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The information to remember. Be specific and self-contained.",
            },
            "category": {
                "type": "string",
                "description": (
                    "Optional category tag (e.g., 'preference', 'fact', "
                    "'decision', 'contact'). Helps organize memories."
                ),
            },
        },
        "required": ["text"],
    }

    def __init__(self, vector_memory: VectorMemory) -> None:
        self._vm = vector_memory

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        text = kwargs["text"]
        category = kwargs.get("category", "")

        row_id = await self._vm.save(
            chat_id=workspace_dir.name,
            text=text,
            category=category,
        )
        cat_str = f" (category: {category})" if category else ""
        return f"✅ Saved to memory [id={row_id}]{cat_str}: {text[:100]}"


# ─── Search ──────────────────────────────────────────────────────────────


class MemorySearchSkill(BaseSkill):
    """Search memories using semantic similarity."""

    name = "memory_search"
    description = (
        "Search long-term memory using natural language. Finds semantically "
        "similar memories, even if exact words differ. Returns the most "
        "relevant stored memories for the current conversation."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for in memory (natural language).",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default: 5).",
            },
        },
        "required": ["query"],
    }

    def __init__(self, vector_memory: VectorMemory) -> None:
        self._vm = vector_memory

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        query = kwargs["query"]
        limit = kwargs.get("limit", 5)

        results = await self._vm.search(
            chat_id=workspace_dir.name,
            query=query,
            limit=limit,
        )

        if not results:
            return "No memories found matching your query."

        lines = [f"🔍 Found {len(results)} memory(es):\n"]
        for r in results:
            cat = f" [{r['category']}]" if r.get("category") else ""
            dist = f" (relevance: {1 - r['distance']:.2f})" if r.get("distance") is not None else ""
            lines.append(
                f"- [id={r['id']}]{cat} {_format_ts(r['created_at'])}{dist}\n  {r['text']}"
            )
        return "\n".join(lines)


# ─── List ────────────────────────────────────────────────────────────────


class MemoryListSkill(BaseSkill):
    """List recent memories for the current chat."""

    name = "memory_list"
    description = (
        "List the most recently saved memories for this conversation. "
        "Useful to review what has been stored and decide if anything "
        "needs updating."
    )
    parameters = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Maximum number of memories to list (default: 10).",
            },
        },
        "required": [],
    }

    def __init__(self, vector_memory: VectorMemory) -> None:
        self._vm = vector_memory

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        limit = kwargs.get("limit", 10)

        entries = self._vm.list_recent(
            chat_id=workspace_dir.name,
            limit=limit,
        )
        total = self._vm.count(chat_id=workspace_dir.name)

        if not entries:
            return "No memories stored yet for this conversation."

        lines = [f"📋 {len(entries)} recent memories (total: {total}):\n"]
        for e in entries:
            cat = f" [{e['category']}]" if e.get("category") else ""
            lines.append(f"- [id={e['id']}]{cat} {_format_ts(e['created_at'])}\n  {e['text']}")
        return "\n".join(lines)
