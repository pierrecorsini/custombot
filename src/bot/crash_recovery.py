"""
crash_recovery.py — Recover stale pending messages after a crash.

Called during bot startup to handle messages that were interrupted
by a crash or restart. Validates sender ACL, reconstructs messages,
and re-processes them through the normal message pipeline.

Recovered messages are batched (up to *max_concurrent_messages*)
and processed concurrently via ``asyncio.gather`` to reduce
event-loop scheduling overhead after long downtimes.  Per-chat
ordering is preserved by the per-chat lock inside ``handle_message``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from src.channels.base import IncomingMessage
from src.constants import DEFAULT_MAX_CONCURRENT_MESSAGES

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
    from src.message_queue import MessageQueue, QueuedMessage

log = logging.getLogger(__name__)

# Protocol for the message handler callback — avoids circular import of Bot.
from typing import Protocol


class MessageHandler(Protocol):
    async def __call__(self, msg: IncomingMessage) -> str | None: ...


# ── per-message recovery logic ────────────────────────────────────────────────


def _resolve_sender_id(queued_msg: QueuedMessage) -> str | None:
    """Return the best available sender identifier, or *None* if absent."""
    if (
        hasattr(queued_msg, "sender_id")
        and isinstance(queued_msg.sender_id, str)
        and queued_msg.sender_id
    ):
        return queued_msg.sender_id
    return queued_msg.sender_name or None


def _check_acl(
    queued_msg: QueuedMessage,
    channel: BaseChannel | None,
) -> str | None:
    """Return *None* if the sender passes ACL, otherwise a skip reason string."""
    if channel is None:
        # No channel available — cannot verify ACL
        return "no_channel"

    if hasattr(channel, "_is_allowed"):
        sender_id = _resolve_sender_id(queued_msg) or ""
        if not channel._is_allowed(sender_id):
            return "not_in_allowed_numbers"

    # Channel present (with or without _is_allowed) — proceed
    return None


def _reconstruct_message(queued_msg: QueuedMessage) -> IncomingMessage:
    """Build an ``IncomingMessage`` from persisted queue data."""
    _sender_id = _resolve_sender_id(queued_msg) or "unknown"
    # Sanitize to meet IncomingMessage validation
    _sender_id = re.sub(r"[^a-zA-Z0-9_\-.@]", "_", _sender_id) or "unknown"
    return IncomingMessage(
        message_id=queued_msg.message_id,
        chat_id=queued_msg.chat_id,
        sender_id=_sender_id,
        sender_name=queued_msg.sender_name or "",
        text=queued_msg.text,
        timestamp=queued_msg.created_at or time.time(),
        acl_passed=True,
    )


async def _recover_one(
    queued_msg: QueuedMessage,
    handle_message: MessageHandler,
    channel: BaseChannel | None,
) -> dict[str, Any] | None:
    """Process a single stale message.

    Returns *None* when the message was skipped (ACL), or a result dict:
        ``{"ok": True}`` on success, ``{"ok": False, "message_id": ..., ...}`` on failure.
    """
    # ACL gate
    skip_reason = _check_acl(queued_msg, channel)
    if skip_reason is not None:
        sender_id = _resolve_sender_id(queued_msg) or ""
        if skip_reason == "not_in_allowed_numbers":
            log.warning(
                "Skipping recovery of message %s — sender %s not in allowed_numbers",
                queued_msg.message_id,
                sender_id,
            )
        else:
            log.warning(
                "Skipping recovery of message %s — no channel for ACL check. "
                "Recovery should be called after channel initialization.",
                queued_msg.message_id,
            )
        return None  # skipped

    recovered_msg = _reconstruct_message(queued_msg)

    try:
        await handle_message(recovered_msg)
        log.info(
            "Successfully recovered message %s from chat %s",
            queued_msg.message_id,
            queued_msg.chat_id,
        )
        return {"ok": True}
    except Exception as exc:
        log.error(
            "Failed to recover message %s: %s",
            queued_msg.message_id,
            exc,
            exc_info=True,
        )
        return {
            "ok": False,
            "message_id": queued_msg.message_id,
            "chat_id": queued_msg.chat_id,
            "error": str(exc),
        }


# ── public API ────────────────────────────────────────────────────────────────


async def recover_pending_messages(
    message_queue: MessageQueue,
    handle_message: MessageHandler,
    timeout_seconds: int | None = None,
    channel: BaseChannel | None = None,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_MESSAGES,
) -> dict[str, Any]:
    """Recover and process stale pending messages from previous crash.

    Should be called during bot startup to handle messages that were
    interrupted by a crash or restart.

    Messages are batched (up to *max_concurrent*) and each batch is
    processed concurrently via ``asyncio.gather``.  This reduces
    event-loop scheduling overhead when many messages accumulated
    during a long downtime.

    Args:
        message_queue: The message queue to recover from.
        handle_message: Callback to process each recovered message.
        timeout_seconds: Custom timeout for stale detection (uses queue default if not provided).
        channel: Optional channel for sender ACL validation during recovery.
        max_concurrent: Maximum number of messages to process concurrently per batch.

    Returns:
        dict with keys:
        - total_found: int - total stale messages found
        - recovered: int - successfully recovered count
        - failed: int - failed recovery count
        - failures: list - list of {message_id, chat_id, error} dicts
    """
    stale_messages = await message_queue.recover_stale(timeout_seconds)

    if not stale_messages:
        log.info("No stale messages to recover")
        return {"total_found": 0, "recovered": 0, "failed": 0, "failures": []}

    recovered_count = 0
    failures: list[dict[str, str]] = []
    skipped_acl = 0

    # Process in batches to allow concurrent recovery without
    # overwhelming the event loop or LLM rate limits.
    for batch_start in range(0, len(stale_messages), max_concurrent):
        batch = stale_messages[batch_start : batch_start + max_concurrent]
        log.debug(
            "Recovering batch %d-%d/%d",
            batch_start + 1,
            min(batch_start + max_concurrent, len(stale_messages)),
            len(stale_messages),
        )

        coros = [
            _recover_one(msg, handle_message, channel) for msg in batch
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)

        for result in results:
            # Unexpected exception from gather (shouldn't happen — _recover_one
            # catches all Exceptions, but defensive).
            if isinstance(result, BaseException):
                log.error("Unexpected error during batch recovery: %s", result)
                failures.append({"message_id": "unknown", "chat_id": "unknown", "error": str(result)})
                continue

            if result is None:
                skipped_acl += 1
            elif result.get("ok"):
                recovered_count += 1
            else:
                failures.append(
                    {
                        "message_id": result["message_id"],
                        "chat_id": result["chat_id"],
                        "error": result["error"],
                    }
                )

    log.info(
        "Recovery complete: %d/%d messages recovered, %d failed, %d skipped (ACL)",
        recovered_count,
        len(stale_messages),
        len(failures),
        skipped_acl,
    )

    return {
        "total_found": len(stale_messages),
        "recovered": recovered_count,
        "failed": len(failures),
        "failures": failures,
    }
