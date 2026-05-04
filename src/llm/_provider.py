"""
llm._provider — Protocol interface for LLM providers.

Defines the :class:`LLMProvider` protocol that decouples consumers (bot, skills,
lifecycle) from the concrete :class:`~src.llm._client.LLMClient` implementation.

Any class satisfying this interface can serve as the LLM backend — the
OpenAI-based ``LLMClient``, a lightweight test stub, or a future
non-OpenAI-compatible adapter.

:class:`TokenUsage` is co-located here because it is part of the provider
interface contract, not an implementation detail of the OpenAI client.

Usage::

    from src.llm import LLMProvider, TokenUsage   # Protocol + value type
    from src.llm import LLMClient                 # concrete implementation
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import (
    TYPE_CHECKING,
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    runtime_checkable,
)

if TYPE_CHECKING:
    from src.config import LLMConfig

from src.utils.locking import ThreadLock

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
    _lock: ThreadLock = field(default_factory=ThreadLock, repr=False)
    # Per-chat token tracking — bounded LRU with half-eviction policy.
    _per_chat: dict[str, dict[str, int]] = field(
        default_factory=lambda: _make_per_chat_map(),
        repr=False,
    )
    # Pre-sorted leaderboard: (total_tokens, chat_id) ascending.
    # Updated incrementally in add_for_chat() via bisect — O(log n) search.
    _leaderboard: list[tuple[int, str]] = field(default_factory=list, repr=False)
    # Reverse index: chat_id -> set of totals present in _leaderboard.
    # Enables O(k·log n) purge instead of O(n) full scan.
    _leaderboard_idx: dict[str, set[int]] = field(default_factory=dict, repr=False)

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
                # Remove stale leaderboard entry before updating total.
                old_total = self._per_chat[chat_id]["total"]
                _remove_leaderboard_entry(
                    self._leaderboard,
                    self._leaderboard_idx,
                    old_total,
                    chat_id,
                )
                entry = self._per_chat[chat_id]
                entry["prompt"] += prompt
                entry["completion"] += completion
                entry["total"] += prompt + completion
                # Move to end for LRU ordering
                self._per_chat[chat_id] = entry
            else:
                # Evict any stale leaderboard entries left by LRU eviction.
                _purge_chat_from_leaderboard(
                    self._leaderboard,
                    self._leaderboard_idx,
                    chat_id,
                )
                self._per_chat[chat_id] = {
                    "prompt": prompt,
                    "completion": completion,
                    "total": prompt + completion,
                }
            # Insert updated entry — bisect maintains ascending order.
            new_total = self._per_chat[chat_id]["total"]
            bisect.insort(self._leaderboard, (new_total, chat_id))
            self._leaderboard_idx.setdefault(chat_id, set()).add(new_total)

    def get_top_chats(self, n: int = 10) -> list[dict[str, Any]]:
        """Return top-N chats by total token usage, descending.

        Reads from the pre-sorted leaderboard (O(n) slice) instead of
        sorting the entire per-chat dict on every call.
        Stale entries from LRU eviction are skipped lazily.
        """
        with self._lock:
            result: list[dict[str, Any]] = []
            for _total, chat_id in reversed(self._leaderboard):
                if chat_id in self._per_chat:
                    result.append({"chat_id": chat_id, **self._per_chat[chat_id]})
                    if len(result) == n:
                        break
            return result


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


def _remove_leaderboard_entry(
    board: list[tuple[int, str]],
    ridx: dict[str, set[int]],
    total: int,
    chat_id: str,
) -> None:
    """Remove ``(total, chat_id)`` from *board* via bisect lookup.

    Also updates the reverse index *ridx* to keep it consistent.
    """
    key = (total, chat_id)
    i = bisect.bisect_left(board, key)
    if i < len(board) and board[i] == key:
        board.pop(i)
        if chat_id in ridx:
            ridx[chat_id].discard(total)
            if not ridx[chat_id]:
                del ridx[chat_id]


def _purge_chat_from_leaderboard(
    board: list[tuple[int, str]],
    ridx: dict[str, set[int]],
    chat_id: str,
) -> None:
    """Remove **all** entries for *chat_id* from *board*.

    Uses the reverse index for O(k·log n) lookup instead of O(n) scan.
    """
    totals = ridx.pop(chat_id, None)
    if not totals:
        return
    for total in totals:
        key = (total, chat_id)
        i = bisect.bisect_left(board, key)
        if i < len(board) and board[i] == key:
            board.pop(i)


# ── Protocol ─────────────────────────────────────────────────────────────


@runtime_checkable
class LLMProvider(Protocol):
    """Interface that any LLM backend must satisfy.

    Consumers should depend on this Protocol rather than the concrete
    :class:`~src.llm._client.LLMClient` to allow testing with lightweight stubs
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

    def update_config(self, new_cfg: LLMConfig) -> None:
        """Update the LLM config with validation.

        Validates the new config (temperature bounds, non-empty model name,
        etc.) and applies it.  Implementations may also adjust derived
        resources (e.g. httpx timeout) to match the new configuration.
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
