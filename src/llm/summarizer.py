"""
llm.summarizer — Conversation summarization for long contexts.

When chat history exceeds the token budget, compresses older turns into
a condensed summary block via a dedicated LLM call.  Caches summaries
by message-hash so unchanged history is never re-summarized.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.constants import DEFAULT_CONTEXT_TOKEN_BUDGET

if TYPE_CHECKING:
    from src.core.context_builder import ChatMessage
    from src.llm._provider import LLMProvider

log = logging.getLogger(__name__)

_SUMMARY_SYSTEM = (
    "You are a conversation summarizer. Produce a concise, factual "
    "summary of the conversation below. Preserve key decisions, facts, "
    "and context needed to continue the conversation. Do NOT add "
    "commentary. Write in the same language as the conversation."
)

_DEFAULT_TRIGGER_TOKENS = 3_000


@dataclass(slots=True)
class ConversationSummarizer:
    """Compresses old chat messages into a short summary via LLM.

    Caches the summary keyed on a hash of the summarised messages so
    that repeated calls with unchanged history return the cached result
    without an extra LLM round-trip.
    """

    llm: LLMProvider
    enabled: bool = True
    trigger_tokens: int = _DEFAULT_TRIGGER_TOKENS
    _cache: dict[str, str] = field(default_factory=dict, repr=False)

    # ── public API ───────────────────────────────────────────────────────

    async def summarize_history(
        self,
        messages: list[ChatMessage],
        target_tokens: int,
    ) -> str:
        """Summarise *messages* to fit within *target_tokens*.

        If the total estimated tokens of *messages* are already within
        the target, returns an empty string (no summarisation needed).

        Returns the cached summary when the input messages haven't
        changed since the last call.
        """
        if not messages or not self.enabled:
            return ""

        total_tokens = sum(m._cached_tokens for m in messages)
        if total_tokens <= target_tokens:
            return ""

        cache_key = self._hash_messages(messages)
        cached = self._cache.get(cache_key)
        if cached is not None:
            log.debug("Summarizer cache hit (%d messages)", len(messages))
            return cached

        summary = await self._call_llm(messages)
        self._cache[cache_key] = summary

        # Bounded cache: evict oldest when too large.
        if len(self._cache) > 64:
            oldest = next(iter(self._cache))
            del self._cache[oldest]

        log.info(
            "Summarised %d messages (%d tokens) into %d-char summary",
            len(messages),
            total_tokens,
            len(summary),
        )
        return summary

    def clear_cache(self) -> None:
        """Drop all cached summaries."""
        self._cache.clear()

    # ── internals ────────────────────────────────────────────────────────

    async def _call_llm(self, messages: list[ChatMessage]) -> str:
        """Invoke the LLM to produce a summary of *messages*."""
        parts: list[str] = []
        for m in messages:
            prefix = m.role.capitalize()
            parts.append(f"{prefix}: {m.content}")

        conversation_text = "\n\n".join(parts)
        api_messages = [
            {"role": "system", "content": _SUMMARY_SYSTEM},
            {"role": "user", "content": f"Summarize this conversation:\n\n{conversation_text}"},
        ]

        response = await self.llm.chat(api_messages)
        content = response.choices[0].message.content
        return (content or "").strip()

    @staticmethod
    def _hash_messages(messages: list[ChatMessage]) -> str:
        """Stable hash of message contents for cache keying."""
        h = hashlib.sha256()
        for m in messages:
            h.update(m.role.encode())
            h.update(m.content.encode())
        return h.hexdigest()[:16]
