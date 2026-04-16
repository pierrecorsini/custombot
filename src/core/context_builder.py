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
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from src.config import Config
    from src.db import Database

from src.core.topic_cache import META_PROMPT
from src.security.prompt_injection import (
    check_system_prompt_length,
    detect_injection,
    sanitize_user_input,
    DEFAULT_MAX_SYSTEM_PROMPT_LENGTH,
)

log = logging.getLogger(__name__)

# When a topic summary exists, fetch fewer messages (tokens saved by summary).
_REDUCED_HISTORY_FRACTION = 3


async def build_context(
    db: "Database",
    config: "Config",
    chat_id: str,
    memory_content: str | None,
    agents_md: str,
    instruction: str = "",
    channel_prompt: str | None = None,
    project_context: str | None = None,
    topic_summary: str | None = None,
) -> list[dict[str, Any]]:
    """
    Build the LLM message context for a chat.

    Args:
        db: Database for fetching message history.
        config: Configuration with system prompt and settings.
        chat_id: Chat identifier.
        memory_content: Optional memory notes from previous conversations.
        agents_md: AGENTS.md content from workspace.
        instruction: Routed instruction content.
        channel_prompt: Optional channel-specific prompt.
        project_context: Optional project knowledge context for injection.
        topic_summary: Cached summary from a previous topic change.

    Returns:
        List of message dicts for LLM API call.
    """
    system_parts = []
    if config.llm.system_prompt_prefix and config.llm.system_prompt_prefix.strip():
        system_parts.append(config.llm.system_prompt_prefix.strip())

    # Inject channel-specific prompt first (highest priority)
    if channel_prompt and channel_prompt.strip():
        system_parts.append("\n---\n" + channel_prompt.strip())

    # Inject routed instruction (high priority)
    if instruction.strip():
        system_parts.append("\n---\n## 📋 Instructions\n\n" + instruction.strip())

    if agents_md.strip():
        extra = agents_md.strip()
        if extra != config.llm.system_prompt_prefix:
            system_parts.append("\n---\n" + extra)

    if memory_content:
        system_parts.append(
            "\n---\n## 📝 Memory (notes from previous conversations)\n\n"
            + memory_content.strip()
        )

    # Inject cached topic summary (replaces old history)
    if topic_summary:
        system_parts.append(
            "\n---\n## 📋 Previous Conversation Summary\n\n" + topic_summary.strip()
        )

    if project_context:
        system_parts.append(
            "\n---\n## 📂 Project Context\n\n" + project_context.strip()
        )

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

    system_msg = {"role": "system", "content": system_content}

    # Reduce history fetch when topic summary exists (saves tokens)
    history_limit = config.memory_max_history
    if topic_summary:
        history_limit = max(10, history_limit // _REDUCED_HISTORY_FRACTION)

    rows = await db.get_recent_messages(chat_id, history_limit)
    history = db_rows_to_messages(rows)

    # Sanitize user messages in history to prevent delayed injection
    sanitized_history = _sanitize_history(history)

    return [system_msg] + sanitized_history


def db_rows_to_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert DB rows to OpenAI message dicts."""
    messages = []
    for row in rows:
        role = row["role"]
        content = row["content"]
        match role:
            case "user":
                messages.append({"role": "user", "content": content})
            case "assistant":
                messages.append({"role": "assistant", "content": content})
            # tool / system rows are not re-injected into history
    return messages


def _sanitize_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sanitize user messages in history for injection prevention.

    Messages flagged with _sanitized=True were already scanned at save time
    (see db.py save_message). Only scan unflagged messages here (e.g. messages
    saved before this optimization was added).
    """
    sanitized = []
    for msg in messages:
        if msg.get("role") == "user" and not msg.get("_sanitized"):
            result = detect_injection(msg["content"])
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
                    {
                        "role": "user",
                        "content": sanitize_user_input(msg["content"]),
                    }
                )
                continue
            elif result.detected:
                log.info(
                    "Low-confidence injection detected (not sanitized): confidence=%.1f patterns=%s",
                    result.confidence,
                    result.matched_patterns,
                )
        sanitized.append(msg)
    return sanitized
