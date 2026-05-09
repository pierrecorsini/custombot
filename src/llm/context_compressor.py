"""
llm.context_compressor — Semantic deduplication of context messages.

Before sending context to the LLM, removes semantically similar past
messages using cosine similarity over embeddings.  Falls back to simple
text-hash dedup when vector memory is unavailable.
"""

from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openai.types.chat import ChatCompletionMessageParam
    from src.vector_memory import VectorMemory

log = logging.getLogger(__name__)

DEFAULT_DEDUP_THRESHOLD = 0.95


@dataclass(slots=True, frozen=True)
class CompressionStats:
    """Statistics from a context compression pass."""

    original_count: int
    compressed_count: int
    duplicates_removed: int
    method: str  # "embedding" or "hash"


class ContextCompressor:
    """Deduplicate semantically similar messages before LLM context assembly.

    Uses embedding-based cosine similarity when vector memory is available,
    falling back to text-hash dedup otherwise.
    """

    def __init__(
        self,
        *,
        vector_memory: VectorMemory | None = None,
        enabled: bool = True,
        threshold: float = DEFAULT_DEDUP_THRESHOLD,
    ) -> None:
        self._vector_memory = vector_memory
        self._enabled = enabled
        self._threshold = threshold

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def compress(
        self,
        messages: list[ChatCompletionMessageParam],
        chat_id: str,
    ) -> tuple[list[ChatCompletionMessageParam], CompressionStats | None]:
        """Remove duplicate messages, keeping the most recent one.

        Returns the deduplicated message list and optional stats.
        """
        if not self._enabled or len(messages) <= 1:
            return messages, None

        if self._vector_memory is not None:
            return await self._compress_via_embeddings(messages, chat_id)

        return self._compress_via_hash(messages)

    async def _compress_via_embeddings(
        self,
        messages: list[ChatCompletionMessageParam],
        chat_id: str,
    ) -> tuple[list[ChatCompletionMessageParam], CompressionStats]:
        """Deduplicate using embedding cosine similarity."""
        texts = [_extract_text(m) for m in messages]
        embeddings: list[list[float] | None] = []

        for text in texts:
            if not text:
                embeddings.append(None)
                continue
            try:
                emb = await self._vector_memory._embed(text)  # type: ignore[union-attr]
                embeddings.append(emb)
            except Exception:
                embeddings.append(None)

        # Walk backwards (most recent first) to keep newest duplicates
        seen: list[tuple[list[float], int]] = []
        keep_indices: list[int] = []

        for i in range(len(messages) - 1, -1, -1):
            emb = embeddings[i]
            if emb is None:
                keep_indices.append(i)
                continue

            is_dup = False
            for existing_emb, existing_idx in seen:
                if _cosine_similarity(emb, existing_emb) >= self._threshold:
                    is_dup = True
                    log.debug(
                        "Dedup: message %d is duplicate of %d (chat=%s)",
                        i,
                        existing_idx,
                        chat_id,
                    )
                    break

            if is_dup:
                continue

            keep_indices.append(i)
            seen.append((emb, i))

        # Restore original order
        keep_indices.sort()
        result = [messages[i] for i in keep_indices]
        removed = len(messages) - len(result)

        return result, CompressionStats(
            original_count=len(messages),
            compressed_count=len(result),
            duplicates_removed=removed,
            method="embedding",
        )

    def _compress_via_hash(
        self,
        messages: list[ChatCompletionMessageParam],
    ) -> tuple[list[ChatCompletionMessageParam], CompressionStats]:
        """Fallback: deduplicate using normalized text hash."""
        seen_hashes: set[str] = set()
        # Walk backwards to keep newest
        keep: list[tuple[int, ChatCompletionMessageParam]] = []

        for i in range(len(messages) - 1, -1, -1):
            text = _extract_text(messages[i])
            h = _text_hash(text)
            if h in seen_hashes:
                continue
            seen_hashes.add(h)
            keep.append((i, messages[i]))

        keep.sort(key=lambda t: t[0])
        result = [m for _, m in keep]
        removed = len(messages) - len(result)

        return result, CompressionStats(
            original_count=len(messages),
            compressed_count=len(result),
            duplicates_removed=removed,
            method="hash",
        )


def _extract_text(message: ChatCompletionMessageParam) -> str:
    """Extract plain text content from a message dict."""
    content = message.get("content", "")
    if isinstance(content, str):
        return content.strip().lower()
    return ""


def _text_hash(text: str) -> str:
    """Normalized hash for text dedup (whitespace-collapsed, lowercased)."""
    normalized = " ".join(text.lower().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)
