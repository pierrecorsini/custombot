"""
llm_provider.py — Protocol interface for LLM providers.

Defines the :class:`LLMProvider` protocol that decouples consumers (bot, skills,
lifecycle) from the concrete :class:`~src.llm.LLMClient` implementation.

Any class satisfying this interface can serve as the LLM backend — the
OpenAI-based ``LLMClient``, a lightweight test stub, or a future
non-OpenAI-compatible adapter.

:class:`TokenUsage` is co-located here because it is part of the provider
interface contract, not an implementation detail of the OpenAI client.

Usage::

    from src.llm_provider import LLMProvider, TokenUsage   # Protocol + value type
    from src.llm import LLMClient                          # concrete implementation
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openai import AsyncOpenAI
    from openai.types.chat import (
        ChatCompletion,
        ChatCompletionMessageParam,
        ChatCompletionToolParam,
    )
    from src.utils.circuit_breaker import CircuitBreaker


# ── Value types ──────────────────────────────────────────────────────────


@dataclass(slots=True)
class TokenUsage:
    """Token usage statistics for LLM API calls.

    Thread-safe: all mutations are guarded by an internal lock.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    # Per-chat token tracking — bounded LRU with half-eviction policy.
    _per_chat: dict[str, dict[str, int]] = field(
        default_factory=lambda: _make_per_chat_map(),
        repr=False,
    )

    def to_dict(self) -> Dict[str, int]:
        """Convert to dictionary for serialization."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
        }

    def add(self, prompt: int, completion: int) -> None:
        """Add token usage from a single request (thread-safe)."""
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += prompt + completion
            self.request_count += 1

    def add_for_chat(self, chat_id: str, prompt: int, completion: int) -> None:
        """Add per-chat token usage (thread-safe, bounded LRU with half-eviction)."""
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += prompt + completion
            self.request_count += 1
            if chat_id in self._per_chat:
                entry = self._per_chat[chat_id]
                entry["prompt"] += prompt
                entry["completion"] += completion
                entry["total"] += prompt + completion
                # Move to end for LRU ordering
                self._per_chat[chat_id] = entry
            else:
                self._per_chat[chat_id] = {
                    "prompt": prompt,
                    "completion": completion,
                    "total": prompt + completion,
                }

    def get_top_chats(self, n: int = 10) -> list[dict[str, Any]]:
        """Return top-N chats by total token usage, descending."""
        with self._lock:
            sorted_chats = sorted(
                self._per_chat.items(),
                key=lambda item: item[1]["total"],
                reverse=True,
            )
            return [
                {"chat_id": cid, **stats}
                for cid, stats in sorted_chats[:n]
            ]


def _make_per_chat_map() -> dict[str, dict[str, int]]:
    """Factory for the per-chat LRU tracking dict.

    Uses ``BoundedOrderedDict`` when available (production); falls back to a
    plain ``dict`` for isolated test environments that only exercise ``TokenUsage``
    without importing the full utils package.
    """
    try:
        from src.utils import BoundedOrderedDict
        return BoundedOrderedDict(max_size=1000, eviction="half")  # type: ignore[arg-type]
    except ImportError:
        return {}


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class LLMProvider(Protocol):
    """Interface that any LLM backend must satisfy.

    Consumers should depend on this Protocol rather than the concrete
    :class:`~src.llm.LLMClient` to allow testing with lightweight stubs
    and to make it straightforward to add non-OpenAI-compatible providers.

    The Protocol is self-contained: all type dependencies (``TokenUsage``,
    ``CircuitBreaker``, OpenAI message types) are imported under
    ``TYPE_CHECKING`` so that alternative implementations do not need to
    import ``src.llm`` at all.
    """

    @property
    def token_usage(self) -> TokenUsage: ...

    @property
    def circuit_breaker(self) -> CircuitBreaker: ...

    @property
    def openai_client(self) -> AsyncOpenAI:
        """Underlying OpenAI client for embeddings / models.list().

        Providers that do not use the OpenAI SDK should raise
        ``NotImplementedError``.  This accessor exists so that
        ``VectorMemory`` can obtain an embeddings client without
        coupling to ``LLMClient`` internals.
        """
        ...

    async def warmup(self) -> bool: ...

    async def chat(
        self,
        messages: List[ChatCompletionMessageParam],
        tools: Optional[List[ChatCompletionToolParam]] = None,
        timeout: Optional[float] = None,
        chat_id: Optional[str] = None,
    ) -> ChatCompletion: ...

    async def chat_stream(
        self,
        messages: List[ChatCompletionMessageParam],
        tools: Optional[List[ChatCompletionToolParam]] = None,
        timeout: Optional[float] = None,
        on_chunk: Optional[Callable[[str], Awaitable[None]]] = None,
        chat_id: Optional[str] = None,
    ) -> ChatCompletion: ...

    async def close(self) -> None: ...
