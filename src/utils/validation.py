"""
src/utils/validation.py — Shared input validation functions.

Consolidates validation logic used across multiple packages
(db, channels, bot) into a single canonical source, eliminating
duplicate implementations with divergent patterns and error messages.
"""

from __future__ import annotations

import re

from src.constants import MAX_CHAT_ID_LENGTH

# Pattern for valid chat_id (safe for file paths and message boundaries).
# Allows: alphanumeric, dash, underscore, dot, and @.
# Real-world values: "1234567890@s.whatsapp.net", "120363abc@g.us",
# "12345678-1234-1234-1234-123456789012" (CLI UUID).
_CHAT_ID_RE = re.compile(r"^[a-zA-Z0-9_\-.@]+$")


def _validate_chat_id(chat_id: object, *, max_length: int = 200) -> None:
    """Validate ``chat_id`` for safe use in filesystem paths and message routing.

    Defense-in-depth check that catches malicious or malformed chat IDs
    before they reach any filesystem operation (workspace directories,
    JSONL files, scheduler paths, etc.) or message processing.

    Args:
        chat_id: Value to validate.
        max_length: Maximum allowed length (default matches MAX_CHAT_ID_LENGTH).

    Raises:
        TypeError: If ``chat_id`` is not a string.
        ValueError: If ``chat_id`` is empty, too long, or contains unsafe characters.
    """
    if not isinstance(chat_id, str):
        raise TypeError(f"chat_id must be a str, got {type(chat_id).__name__}")
    if not chat_id:
        raise ValueError("chat_id must not be empty")
    effective_max = max_length or MAX_CHAT_ID_LENGTH
    if len(chat_id) > effective_max:
        raise ValueError(
            f"chat_id exceeds maximum length "
            f"({len(chat_id)} > {effective_max}): {chat_id[:40]!r}..."
        )
    if not _CHAT_ID_RE.match(chat_id):
        raise ValueError(
            f"chat_id contains invalid characters: {chat_id!r}. "
            "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
        )
