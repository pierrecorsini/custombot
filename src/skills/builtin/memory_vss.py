"""
src/skills/builtin/memory_vss.py — Vector memory skills (sqlite-vec).

Three LLM-callable tools for semantic memory:
  • MemorySaveSkill   — Store a note with auto-generated embedding
  • MemorySearchSkill — Semantic similarity search within chat memories
  • MemoryListSkill   — List recent memories for this chat

Graceful degradation
--------------------
When ``vector_memory`` is ``None`` (startup failure, sqlite-vec unavailable)
or when the embedding API is unreachable mid-session, all three skills fall
back to text-based operations over ``MEMORY.md`` — regex/keyword search for
``memory_search``, direct file read for ``memory_list``, and an informative
message for ``memory_save``.  This ensures memory functionality is never
completely absent even when the vector store is down.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

from src.core.errors import NonCriticalCategory, log_noncritical
from src.skills.base import BaseSkill, validate_input

if TYPE_CHECKING:
    from src.vector_memory import VectorMemory
    from pathlib import Path

log = logging.getLogger(__name__)

# Marker prefix appended to results that came from the text-based fallback
# rather than semantic vector search.  Helps the LLM understand the result
# quality may differ.
_FALLBACK_MARKER = "📋 (text-based fallback — vector store unavailable)"


def _format_ts(epoch: float) -> str:
    """Format a Unix timestamp for display."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


# ── text-based fallback helpers ──────────────────────────────────────────


async def _read_memory_md(workspace_dir: "Path") -> str | None:
    """Read MEMORY.md from the chat workspace directory.

    Returns the file content or ``None`` if the file does not exist.
    File I/O is offloaded to a thread to avoid blocking the event loop.
    """
    from pathlib import Path

    memory_path = workspace_dir / "MEMORY.md"
    try:
        content = await asyncio.to_thread(memory_path.read_text, encoding="utf-8")
        return content.strip() or None
    except FileNotFoundError:
        return None
    except OSError as exc:
        log_noncritical(
            NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
            "Failed to read MEMORY.md for text-based fallback: %s",
            exc,
            logger=log,
        )
        return None


def _keyword_search(text: str, query: str, limit: int) -> list[dict[str, Any]]:
    """Simple keyword / regex search over plain text.

    Splits *text* into non-empty lines, then matches each line
    case-insensitively against every word in *query*.  Lines matching
    at least one query term are returned as result dicts (up to *limit*).

    Returns a list of dicts with keys ``text``, ``line``, ``score``
    (number of matching query terms).
    """
    # Tokenize query into individual words for matching
    query_terms = [re.escape(w) for w in query.lower().split() if len(w) >= 2]
    if not query_terms:
        return []

    results: list[dict[str, Any]] = []
    lines = text.splitlines()

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        lower = stripped.lower()
        score = sum(1 for term in query_terms if re.search(term, lower))
        if score > 0:
            results.append(
                {
                    "text": stripped,
                    "line": idx + 1,
                    "score": score,
                }
            )

    # Sort by relevance (most matching terms first), then take top *limit*
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


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

    def __init__(self, vector_memory: "VectorMemory | None" = None) -> None:
        self._vm = vector_memory

    @validate_input
    async def execute(self, workspace_dir: "Path", **kwargs: Any) -> str:
        text = kwargs["text"]
        category = kwargs.get("category", "")

        if self._vm is not None:
            row_id = await self._vm.save(
                chat_id=workspace_dir.name,
                text=text,
                category=category,
            )
            cat_str = f" (category: {category})" if category else ""
            return f"✅ Saved to memory [id={row_id}]{cat_str}: {text[:100]}"

        # Fallback: vector store unavailable — inform the user.
        log_noncritical(
            NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
            "memory_save falling back: vector_memory is None for workspace=%s",
            workspace_dir.name,
            logger=log,
            level=logging.WARNING,
        )
        cat_str = f" (category: {category})" if category else ""
        return (
            f"⚠️ Vector memory unavailable — information not persisted to "
            f"long-term memory{cat_str}: {text[:100]}. "
            f"Vector memory will be available once the service recovers."
        )


# ─── Search ──────────────────────────────────────────────────────────────


class MemorySearchSkill(BaseSkill):
    """Search memories using semantic similarity, with text-based fallback."""

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

    def __init__(self, vector_memory: "VectorMemory | None" = None) -> None:
        self._vm = vector_memory

    @validate_input
    async def execute(self, workspace_dir: "Path", **kwargs: Any) -> str:
        query = kwargs["query"]
        limit = kwargs.get("limit", 5)

        # --- Try vector search first (primary path) ---
        if self._vm is not None:
            try:
                results = await self._vm.search(
                    chat_id=workspace_dir.name,
                    query=query,
                    limit=limit,
                )
                if results:
                    return self._format_vector_results(results)
            except Exception as exc:
                log_noncritical(
                    NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
                    "Vector search failed, falling back to text search: %s",
                    exc,
                    logger=log,
                    level=logging.WARNING,
                )

        # --- Fallback: text-based search over MEMORY.md ---
        return await self._text_fallback_search(workspace_dir, query, limit)

    @staticmethod
    def _format_vector_results(results: list[dict[str, Any]]) -> str:
        """Format vector search results for the LLM."""
        lines = [f"🔍 Found {len(results)} memory(es):\n"]
        for r in results:
            cat = f" [{r['category']}]" if r.get("category") else ""
            dist = (
                f" (relevance: {1 - r['distance']:.2f})"
                if r.get("distance") is not None
                else ""
            )
            lines.append(
                f"- [id={r['id']}]{cat} {_format_ts(r['created_at'])}{dist}\n  {r['text']}"
            )
        return "\n".join(lines)

    async def _text_fallback_search(
        self, workspace_dir: "Path", query: str, limit: int
    ) -> str:
        """Search MEMORY.md using keyword matching as a fallback."""
        log_noncritical(
            NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
            "Using text-based memory search fallback for workspace=%s query=%r",
            workspace_dir.name,
            query[:50],
            logger=log,
        )

        content = await _read_memory_md(workspace_dir)
        if not content:
            return "No memories found matching your query."

        matches = _keyword_search(content, query, limit)
        if not matches:
            return "No memories found matching your query."

        lines = [f"🔍 Found {len(matches)} match(es) {_FALLBACK_MARKER}:\n"]
        for m in matches:
            lines.append(f"  [line {m['line']}] {m['text']}")
        return "\n".join(lines)


# ─── List ────────────────────────────────────────────────────────────────


class MemoryListSkill(BaseSkill):
    """List recent memories for the current chat, with text-based fallback."""

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

    def __init__(self, vector_memory: "VectorMemory | None" = None) -> None:
        self._vm = vector_memory

    @validate_input
    async def execute(self, workspace_dir: "Path", **kwargs: Any) -> str:
        limit = kwargs.get("limit", 10)

        # --- Try vector list first (primary path) ---
        if self._vm is not None:
            try:
                entries = self._vm.list_recent(
                    chat_id=workspace_dir.name,
                    limit=limit,
                )
                total = self._vm.count(chat_id=workspace_dir.name)

                if entries:
                    lines = [f"📋 {len(entries)} recent memories (total: {total}):\n"]
                    for e in entries:
                        cat = f" [{e['category']}]" if e.get("category") else ""
                        lines.append(
                            f"- [id={e['id']}]{cat} {_format_ts(e['created_at'])}\n  {e['text']}"
                        )
                    return "\n".join(lines)
            except Exception as exc:
                log_noncritical(
                    NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
                    "Vector list_recent failed, falling back to text: %s",
                    exc,
                    logger=log,
                    level=logging.WARNING,
                )

        # --- Fallback: read MEMORY.md content ---
        return await self._text_fallback_list(workspace_dir, limit)

    async def _text_fallback_list(
        self, workspace_dir: "Path", limit: int
    ) -> str:
        """List contents of MEMORY.md as a fallback."""
        log_noncritical(
            NonCriticalCategory.VECTOR_MEMORY_FALLBACK,
            "Using text-based memory list fallback for workspace=%s",
            workspace_dir.name,
            logger=log,
        )

        content = await _read_memory_md(workspace_dir)
        if not content:
            return "No memories stored yet for this conversation."

        lines = content.splitlines()
        non_empty = [l.strip() for l in lines if l.strip()]
        display = non_empty[:limit]

        if not display:
            return "No memories stored yet for this conversation."

        result_lines = [f"📋 {len(display)} entries {_FALLBACK_MARKER}:\n"]
        for entry in display:
            result_lines.append(f"  {entry}")
        return "\n".join(result_lines)
