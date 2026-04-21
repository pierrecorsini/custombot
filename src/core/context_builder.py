"""
src/core/context_builder.py — Context building logic for LLM conversations.

Constructs the message list including:
- System prompt
- Channel-specific prompts
- Instructions
- Memory content
- Topic summary (cached from previous topic changes)
- Message history (reduced when topic summary exists)
- Topic detection META instruction
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.bot import BotConfig
    from src.db import Database

from src.constants import CHARS_PER_TOKEN, CJK_CHARS_PER_TOKEN, DEFAULT_CONTEXT_TOKEN_BUDGET
from src.core.topic_cache import META_PROMPT
from src.security.prompt_injection import (
    DEFAULT_MAX_SYSTEM_PROMPT_LENGTH,
    check_system_prompt_length,
    detect_injection,
    sanitize_user_input,
)

log = logging.getLogger(__name__)

# Pre-compiled regex for CJK character detection.
# Covers: CJK Unified Ideographs (+ Extension A), Hiragana, Katakana,
# Hangul Syllables, Hangul Jamo, and Fullwidth/Halfwidth forms.
# Using a compiled regex delegates character matching to C level,
# significantly faster than a Python-level per-character loop.
_CJK_RE = re.compile(
    r'[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u309f\u30a0-\u30ff'
    r'\uac00-\ud7af\u1100-\u11ff\uff00-\uffef]'
)


@dataclass(slots=True, frozen=True)
class ChatMessage:
    """Typed chat message for context building.

    Isolates the OpenAI wire format to ``llm.py`` — callers manipulate
    typed objects and convert to dicts only at the LLM boundary via
    ``to_api_dict()``.
    """

    role: str  # "system", "user", or "assistant"
    content: str
    name: str | None = None
    _sanitized: bool = False
    _cached_tokens: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, '_cached_tokens', estimate_tokens(self.content))

    def to_api_dict(self) -> dict[str, Any]:
        """Convert to the OpenAI API wire-format dict."""
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            d["name"] = self.name
        return d

@dataclass
class HistoryBundle:
    """Message history paired with its unsanitized user-message count.

    Tracks the count of unsanitized user-role messages at the list level
    so that ``_sanitize_history()`` can take an O(1) fast-path without
    iterating the entire list.  The counter is decremented automatically
    when messages are dropped by ``_trim_history_to_budget()``.
    """

    messages: list[ChatMessage]
    unsanitized_count: int = 0


# When a topic summary exists, fetch fewer messages (tokens saved by summary).
_REDUCED_HISTORY_FRACTION = 3


def estimate_tokens(text: str) -> int:
    """Estimate token count using a chars-per-token heuristic.

    CJK characters (Chinese, Japanese, Korean) tokenize at ~1.5 chars/token
    versus ~4 for English.  We count CJK characters separately so that
    multilingual conversations produce a more accurate estimate and the
    ``_trim_history_to_budget()`` gate does not allow too many tokens through.

    Uses the pre-compiled ``_CJK_RE`` regex for CJK detection, delegating
    character matching to the C level for O(n) performance with significantly
    lower constant factor than a Python-level per-character loop.
    """
    cjk_count = len(_CJK_RE.findall(text))
    non_cjk = len(text) - cjk_count
    return int(non_cjk / CHARS_PER_TOKEN + cjk_count / CJK_CHARS_PER_TOKEN)


async def build_context(
    db: "Database",
    config: "BotConfig",
    chat_id: str,
    memory_content: str | None,
    agents_md: str,
    instruction: str = "",
    channel_prompt: str | None = None,
    project_context: str | None = None,
    topic_summary: str | None = None,
) -> list[ChatMessage]:
    """
    Build the LLM message context for a chat.

    Args:
        db: Database for fetching message history.
        config: Bot-level config with system prompt and history settings.
        chat_id: Chat identifier.
        memory_content: Optional memory notes from previous conversations.
        agents_md: AGENTS.md content from workspace.
        instruction: Routed instruction content.
        channel_prompt: Optional channel-specific prompt.
        project_context: Optional project knowledge context for injection.
        topic_summary: Cached summary from a previous topic change.

    Returns:
        List of ChatMessage objects for context manipulation.
    """
    system_parts = []
    if config.system_prompt_prefix and config.system_prompt_prefix.strip():
        system_parts.append(config.system_prompt_prefix.strip())

    # Inject channel-specific prompt first (highest priority)
    if channel_prompt and channel_prompt.strip():
        system_parts.append("\n---\n" + channel_prompt.strip())

    # Inject routed instruction (high priority)
    if instruction.strip():
        system_parts.append("\n---\n## 📋 Instructions\n\n" + instruction.strip())

    if agents_md.strip():
        extra = agents_md.strip()
        if extra != config.system_prompt_prefix:
            system_parts.append("\n---\n" + extra)

    if memory_content:
        system_parts.append(
            "\n---\n## 📝 Memory (notes from previous conversations)\n\n" + memory_content.strip()
        )

    # Inject cached topic summary (replaces old history)
    if topic_summary:
        system_parts.append(
            "\n---\n## 📋 Previous Conversation Summary\n\n" + topic_summary.strip()
        )

    if project_context:
        system_parts.append("\n---\n## 📂 Project Context\n\n" + project_context.strip())

    # Topic detection META instruction (always present)
    system_parts.append("\n---\n" + META_PROMPT)

    system_content = "\n".join(system_parts)

    # Guard against context overflow attacks
    within_limit, prompt_length = check_system_prompt_length(system_content)
    if not within_limit:
        log.warning(
            "System prompt truncated from %d to %d chars (max: %d)",
            prompt_length,
            DEFAULT_MAX_SYSTEM_PROMPT_LENGTH,
            DEFAULT_MAX_SYSTEM_PROMPT_LENGTH,
            extra={"chat_id": chat_id, "original_length": prompt_length},
        )
        system_content = system_content[:DEFAULT_MAX_SYSTEM_PROMPT_LENGTH]

    system_msg = ChatMessage(role="system", content=system_content)

    # Reduce history fetch when topic summary exists (saves tokens)
    history_limit = config.memory_max_history
    if topic_summary:
        history_limit = max(10, history_limit // _REDUCED_HISTORY_FRACTION)

    rows = await db.get_recent_messages(chat_id, history_limit)
    bundle = db_rows_to_messages(rows)

    # Trim history to fit within the token budget
    system_tokens = estimate_tokens(system_content)
    bundle = _trim_history_to_budget(bundle, system_tokens)

    # Sanitize user messages in history to prevent delayed injection
    sanitized_history = _sanitize_history(bundle)

    return [system_msg] + sanitized_history


def db_rows_to_messages(
    rows: list[dict[str, Any]],
) -> HistoryBundle:
    """Convert DB rows to ChatMessage objects.

    Returns:
        ``HistoryBundle`` pairing the message list with its unsanitized
        user-message count, enabling O(1) fast-path in downstream calls.
    """
    messages: list[ChatMessage] = []
    unsanitized = 0
    for row in rows:
        role = row["role"]
        content = row["content"]
        match role:
            case "user":
                messages.append(ChatMessage(role="user", content=content))
                unsanitized += 1
            case "assistant":
                messages.append(ChatMessage(role="assistant", content=content))
            # tool / system rows are not re-injected into history
    return HistoryBundle(messages=messages, unsanitized_count=unsanitized)


def _trim_history_to_budget(
    bundle: HistoryBundle,
    system_tokens: int,
) -> HistoryBundle:
    """Trim oldest history messages until total tokens fit within budget.

    Drops from the front (oldest first) to keep the most recent context.
    Decrements ``unsanitized_count`` for any user messages that are dropped.

    Returns:
        Updated ``HistoryBundle`` with trimmed messages and adjusted count.
    """
    messages = bundle.messages
    unsanitized_count = bundle.unsanitized_count
    budget = DEFAULT_CONTEXT_TOKEN_BUDGET
    history_tokens = sum(m._cached_tokens for m in messages)

    if system_tokens + history_tokens <= budget:
        return bundle

    # Calculate how many tokens we need to shed, then drop from the front
    excess = (system_tokens + history_tokens) - budget
    drop_count = 0
    freed = 0
    for msg in messages:
        if freed >= excess:
            break
        freed += msg._cached_tokens
        drop_count += 1

    if drop_count >= len(messages):
        return HistoryBundle(messages=[], unsanitized_count=0)

    # Decrement unsanitized counter for any user messages being dropped
    if unsanitized_count > 0 and drop_count > 0:
        dropped_unsanitized = sum(
            1 for m in messages[:drop_count]
            if m.role == "user" and not m._sanitized
        )
        unsanitized_count -= dropped_unsanitized

    trimmed = messages[drop_count:]
    log.warning(
        "History trimmed from %d to %d messages to fit token budget "
        "(system=%d, budget=%d)",
        len(messages),
        len(trimmed),
        system_tokens,
        budget,
    )
    return HistoryBundle(messages=trimmed, unsanitized_count=unsanitized_count)


def _sanitize_history(
    bundle: HistoryBundle,
) -> list[ChatMessage]:
    """Sanitize user messages in history for injection prevention.

    Messages flagged with _sanitized=True were already scanned at save time
    (see db.py save_message). Only scan unflagged messages here (e.g. messages
    saved before this optimization was added).

    Uses the ``unsanitized_count`` from the ``HistoryBundle`` for an O(1)
    fast-path: when the count is 0, no iteration is needed.

    Args:
        bundle: ``HistoryBundle`` pairing messages with their unsanitized count.

    Once all pre-optimization messages have aged out of history, this function
    effectively becomes a no-op for the _sanitized=True fast path.
    """
    messages = bundle.messages
    unsanitized_count = bundle.unsanitized_count

    # O(1) fast path: no unsanitized user messages → return as-is
    if unsanitized_count == 0:
        return messages

    sanitized = []
    unscanned_count = 0
    for msg in messages:
        if msg.role == "user" and not msg._sanitized:
            unscanned_count += 1
            result = detect_injection(msg.content)
            if result.detected and result.confidence >= 0.8:
                log.warning(
                    "Sanitizing high-confidence injection in user message: %s",
                    result.reason,
                    extra={
                        "confidence": result.confidence,
                        "patterns": result.matched_patterns,
                    },
                )
                sanitized.append(
                    ChatMessage(
                        role="user",
                        content=sanitize_user_input(msg.content),
                    )
                )
                continue
            elif result.detected:
                log.info(
                    "Low-confidence injection detected (not sanitized): confidence=%.1f patterns=%s",
                    result.confidence,
                    result.matched_patterns,
                )
        sanitized.append(msg)
    # Log migration progress: how many messages needed re-scanning
    if unscanned_count > 0:
        log.debug(
            "History sanitization: %d/%d user messages were unflagged (pre-optimization)",
            unscanned_count,
            sum(1 for m in messages if m.role == "user"),
        )
    return sanitized
