"""
topic_cache.py — Per-chat topic summary cache.

Stores a cached summary of previous conversation topics so the LLM
doesn't need the full history on every call. File-based, mtime-cached.

Also contains the META parsing logic for extracting topic-change signals
from LLM responses.
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from pathlib import Path

from src.constants import MAX_LRU_CACHE_SIZE
from src.utils.path import sanitize_path_component

log = logging.getLogger(__name__)

SUMMARY_FILENAME = ".topic_summary.md"

# Regex: matches ---META--- followed by JSON object at end of response
META_PATTERN = re.compile(r"\n---META---\s*(\{.*?\})\s*$", re.DOTALL)

# System prompt instruction appended to every LLM call
META_PROMPT = """\

## Topic Detection

End EVERY response with this exact metadata block:
---META---
{"topic_changed": false}

Set "topic_changed" to true ONLY when the user's current message starts a completely new, unrelated subject. When true:
1. Add "old_topic_summary" with a brief summary of the previous conversation.
2. Respond ONLY to the current message — ignore prior conversation context.

Example when topic changed:
---META---
{"topic_changed": true, "old_topic_summary": "User discussed Italian cooking: fresh pasta techniques, regional sauces, San Marzano tomatoes."}"""


class TopicCache:
    """File-based per-chat topic summary cache with LRU eviction."""

    def __init__(self, workspace_root: str) -> None:
        self._root = Path(workspace_root)
        self._cache: OrderedDict[str, tuple[float, str]] = OrderedDict()
        self._max_size = MAX_LRU_CACHE_SIZE

    def _summary_path(self, chat_id: str) -> Path:
        safe = sanitize_path_component(chat_id)
        return self._root / "whatsapp_data" / safe / SUMMARY_FILENAME

    def _ensure_dir(self, chat_id: str) -> Path:
        d = self._summary_path(chat_id).parent
        d.mkdir(parents=True, exist_ok=True)
        return d

    def read(self, chat_id: str) -> str | None:
        """Read cached topic summary, or None if absent."""
        path = self._summary_path(chat_id)
        if not path.exists():
            return None
        mtime = path.stat().st_mtime
        cached = self._cache.get(chat_id)
        if cached and cached[0] == mtime:
            self._cache.move_to_end(chat_id)
            return cached[1] or None
        content = path.read_text(encoding="utf-8").strip()
        self._cache_put(chat_id, mtime, content)
        return content or None

    def write(self, chat_id: str, summary: str) -> None:
        """Write topic summary for a chat."""
        self._ensure_dir(chat_id)
        path = self._summary_path(chat_id)
        path.write_text(summary.strip() + "\n", encoding="utf-8")
        self._cache.pop(chat_id, None)
        log.info("Topic summary updated for chat %s", chat_id)

    def clear(self, chat_id: str) -> None:
        """Remove cached topic summary."""
        path = self._summary_path(chat_id)
        if path.exists():
            path.unlink()
        self._cache.pop(chat_id, None)

    def _cache_put(self, chat_id: str, mtime: float, content: str) -> None:
        if chat_id in self._cache:
            self._cache.move_to_end(chat_id)
        elif len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[chat_id] = (mtime, content)


# ── META parsing ──────────────────────────────────────────────────────────


def parse_meta(response: str) -> tuple[str, dict | None]:
    """Extract ---META--- block from LLM response.

    Returns (clean_text, meta_dict). If no valid META found,
    returns (original_response, None).
    """
    match = META_PATTERN.search(response)
    if not match:
        return response, None

    clean = response[: match.start()].rstrip()
    try:
        meta = json.loads(match.group(1))
        return clean, meta
    except json.JSONDecodeError:
        log.warning("Failed to parse META JSON from LLM response")
        return response, None
