"""
chunks.py — Streaming response chunking for WhatsApp.

Breaks long responses into WhatsApp-sized chunks (≤ 4096 chars) and
sends them progressively with typing indicators between chunks.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Awaitable, Callable

if TYPE_CHECKING:
    from src.channels.base import BaseChannel

log = logging.getLogger(__name__)

# WhatsApp message character limit.
WHATSAPP_MAX_LENGTH: int = 4096

# Try to split at natural boundaries shorter than the hard limit
# so chunks don't break mid-sentence.
_SOFT_LIMIT: int = 3800

# Callback type for streaming chunk delivery.
SendChunk = Callable[[str], Awaitable[None]]


def chunk_message(text: str, max_length: int = WHATSAPP_MAX_LENGTH) -> list[str]:
    """Split *text* into chunks of at most *max_length* characters.

    Prefers splitting on paragraph boundaries (``\\n\\n``), then
    sentence boundaries (``. ``), then line boundaries (``\\n``).
    Falls back to hard splitting when no boundary is found within the
    window.
    """
    if not text:
        return []

    if len(text) <= max_length:
        return [text]

    soft = min(_SOFT_LIMIT, max_length - 1)
    chunks: list[str] = []

    while text:
        if len(text) <= max_length:
            chunks.append(text)
            break

        split_pos = _find_split_point(text, soft, max_length)
        chunks.append(text[:split_pos].rstrip())
        text = text[split_pos:].lstrip()

    return chunks


def _find_split_point(text: str, soft: int, hard: int) -> int:
    """Find the best position to split *text* between *soft* and *hard*."""
    # 1. Paragraph boundary (double newline)
    pos = _rfind_in_window(text, "\n\n", soft)
    if pos > 0:
        return pos + 2

    # 2. Sentence boundary
    pos = _rfind_in_window(text, ". ", soft)
    if pos > 0:
        return pos + 2

    # 3. Line boundary
    pos = _rfind_in_window(text, "\n", soft)
    if pos > 0:
        return pos + 1

    # 4. Space boundary
    pos = _rfind_in_window(text, " ", soft)
    if pos > 0:
        return pos + 1

    # 5. Hard split at max_length
    return hard


def _rfind_in_window(text: str, needle: str, start: int) -> int:
    """Find *needle* in *text*, searching backwards from *start*."""
    # Search from start towards the beginning to find the last
    # occurrence before position start.
    chunk = text[:start]
    return chunk.rfind(needle)


async def send_chunked(
    chat_id: str,
    text: str,
    channel: BaseChannel,
    max_length: int = WHATSAPP_MAX_LENGTH,
) -> int:
    """Split *text* into chunks and send via *channel* with typing.

    Sends a typing indicator between chunks so the user sees activity.

    Returns the number of chunks sent.
    """
    parts = chunk_message(text, max_length)
    if not parts:
        return 0

    for part in parts[:-1]:
        await channel.send_message(chat_id, part)
        try:
            await channel.send_typing(chat_id)
        except Exception:
            pass  # typing indicator is best-effort

    # Send last chunk without trailing typing indicator
    await channel.send_message(chat_id, parts[-1])

    log.debug("Sent response in %d chunk(s) to chat %s", len(parts), chat_id)
    return len(parts)
