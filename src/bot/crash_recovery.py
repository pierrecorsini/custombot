"""
crash_recovery.py — Recover stale pending messages after a crash.

Called during bot startup to handle messages that were interrupted
by a crash or restart. Validates sender ACL, reconstructs messages,
and re-processes them through the normal message pipeline.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from src.channels.base import IncomingMessage

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
    from src.message_queue import MessageQueue

log = logging.getLogger(__name__)

# Protocol for the message handler callback — avoids circular import of Bot.
from typing import Protocol, Awaitable


class MessageHandler(Protocol):
    async def __call__(self, msg: IncomingMessage) -> str | None: ...


async def recover_pending_messages(
    message_queue: MessageQueue,
    handle_message: MessageHandler,
    timeout_seconds: int | None = None,
    channel: BaseChannel | None = None,
) -> dict[str, Any]:
    """Recover and process stale pending messages from previous crash.

    Should be called during bot startup to handle messages that were
    interrupted by a crash or restart.

    Args:
        message_queue: The message queue to recover from.
        handle_message: Callback to process each recovered message.
        timeout_seconds: Custom timeout for stale detection (uses queue default if not provided).
        channel: Optional channel for sender ACL validation during recovery.

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
    for queued_msg in stale_messages:
        try:
            # Validate sender against current ACL before reprocessing
            if channel is not None and hasattr(channel, "_is_allowed"):
                sender_id = queued_msg.sender_id or queued_msg.sender_name or ""
                if not channel._is_allowed(sender_id):
                    log.warning(
                        "Skipping recovery of message %s — sender %s not in allowed_numbers",
                        queued_msg.message_id,
                        sender_id,
                    )
                    skipped_acl += 1
                    continue
            elif channel is None:
                log.warning(
                    "Skipping recovery of message %s — no channel for ACL check. "
                    "Recovery should be called after channel initialization.",
                    queued_msg.message_id,
                )
                skipped_acl += 1
                continue

            # Reconstruct IncomingMessage from queued data
            recovered_msg = IncomingMessage(
                message_id=queued_msg.message_id,
                chat_id=queued_msg.chat_id,
                sender_id=queued_msg.sender_id or "",
                sender_name=queued_msg.sender_name or "",
                text=queued_msg.text,
                timestamp=queued_msg.created_at or time.time(),
                acl_passed=True,  # ACL already verified above
            )

            # Process the recovered message
            await handle_message(recovered_msg)
            recovered_count += 1
            log.info(
                "Successfully recovered message %s from chat %s",
                queued_msg.message_id,
                queued_msg.chat_id,
            )
        except Exception as exc:
            failures.append(
                {
                    "message_id": queued_msg.message_id,
                    "chat_id": queued_msg.chat_id,
                    "error": str(exc),
                }
            )
            log.error(
                "Failed to recover message %s: %s",
                queued_msg.message_id,
                exc,
                exc_info=True,
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
