"""
response_delivery.py — Post-ReAct response delivery pipeline.

Handles the full delivery pipeline after the ReAct loop produces a raw
response: finalize turn, filter sensitive content, append tool-log summary,
dedup, persist to DB, emit events.

Extracted from :class:`Bot` to isolate response delivery from message
orchestration and make the delivery pipeline independently testable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import EVENT_GENERATION_CONFLICT, Event, EventBus, emit_error_event, get_event_bus
from src.logging import get_correlation_id
from src.security.prompt_injection import filter_response_content

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
    from src.core.context_assembler import ContextAssembler
    from src.core.dedup import DeduplicationService
    from src.core.tool_formatter import ToolLogEntry
    from src.db import Database
    from src.monitoring import PerformanceMetrics


log = logging.getLogger(__name__)

__all__ = ["deliver_response", "send_to_chat"]


def _deduplicate_batch(
    batch: list[dict[str, Any]],
    existing: list[dict],
) -> list[dict[str, Any]]:
    """Remove messages from *batch* that already exist in *existing*.

    Compares by ``(role, content)`` pair — sufficient for detecting
    duplicate writes in the generation-conflict recovery path.
    """
    seen = {(m.get("role"), m.get("content")) for m in existing}
    return [m for m in batch if (m.get("role"), m.get("content")) not in seen]


async def deliver_response(
    chat_id: str,
    raw_response: str,
    tool_log: list[ToolLogEntry],
    buffered_persist: list[dict[str, Any]],
    generation: int,
    verbose: str,
    *,
    context_assembler: ContextAssembler,
    db: Database,
    dedup: DeduplicationService | None,
    channel: BaseChannel | None = None,
) -> str | None:
    """Post-ReAct response delivery: format, dedup, persist, emit.

    Handles the full delivery pipeline after the ReAct loop produces a
    raw response:

    1. Finalize turn (topic extraction via context assembler)
    2. Filter sensitive content from response
    3. Append tool-log summary (when verbose == "summary")
    4. Check outbound dedup — suppress duplicate delivery
    5. Persist assistant message to DB (with generation-conflict detection)
    6. Record outbound dedup + emit ``response_sent`` event via :func:`send_to_chat`

    Returns the final response text, or *None* if suppressed by outbound
    dedup.
    """
    from src.exceptions import DatabaseError
    from src.core.tool_formatter import format_response_with_tool_log

    response_text = context_assembler.finalize_turn(chat_id, raw_response)

    filter_result = filter_response_content(response_text)
    if filter_result.flagged:
        response_text = filter_result.sanitized_content
        log.warning(
            "Filtered sensitive content from LLM response: %s",
            filter_result.categories,
            extra={
                "chat_id": chat_id,
                "filter_categories": filter_result.categories,
            },
        )

    if verbose == "summary" and tool_log:
        response_text = format_response_with_tool_log(response_text, tool_log)

    # Outbound dedup: suppress duplicate responses to the same chat.
    if dedup and dedup.check_outbound_duplicate(chat_id, response_text):
        log.info(
            "Outbound dedup suppressed duplicate response for chat %s",
            chat_id,
            extra={"chat_id": chat_id},
        )
        return None

    batch = [*buffered_persist, {"role": "assistant", "content": response_text}]
    if not db.check_generation(chat_id, generation):
        # Generation conflict: another write landed while we were processing.
        # Re-read recent messages and deduplicate our batch to guarantee
        # consistent JSONL order — avoids interleaving tool/result entries
        # with the concurrent turn's messages.
        current_gen = db.get_generation(chat_id)
        existing = await db.get_recent_messages(
            chat_id, limit=len(batch) * 2 + 10,
        )
        original_size = len(batch)
        batch = _deduplicate_batch(batch, existing)

        if not batch:
            log.info(
                "Generation conflict for chat %s resolved: "
                "all messages already persisted.",
                chat_id,
                extra={"chat_id": chat_id},
            )
            await send_to_chat(chat_id, response_text, dedup=dedup, channel=channel)
            return response_text

        log.warning(
            "Write conflict for chat %s — generation changed during "
            "processing. Re-read %d existing messages, merged batch "
            "from %d to %d entries.",
            chat_id,
            len(existing),
            original_size,
            len(batch),
            extra={"chat_id": chat_id},
        )
        await get_event_bus().emit(
            Event(
                name=EVENT_GENERATION_CONFLICT,
                data={
                    "chat_id": chat_id,
                    "expected_generation": generation,
                    "current_generation": current_gen,
                    "existing_count": len(existing),
                    "merged_batch_size": len(batch),
                },
                source="response_delivery.deliver_response",
                correlation_id=get_correlation_id(),
            )
        )
    try:
        _ids = await db.save_messages_batch(chat_id=chat_id, messages=batch)
    except (OSError, DatabaseError) as exc:
        # Disk full, permission denied, or DB circuit-breaker open.
        # The response is already generated — deliver it to the user
        # even if persistence fails.  Log and emit an event so that
        # monitoring subscribers can track write failures.
        log_noncritical(
            NonCriticalCategory.DB_OPERATION,
            f"Failed to persist response for chat {chat_id}: {exc}",
            logger=log,
        )
        await emit_error_event(
            exc,
            "response_delivery.deliver_response",
            extra_data={
                "chat_id": chat_id,
                "source": "response_delivery.deliver_response.save_messages_batch",
            },
        )

    # Record outbound dedup + emit response_sent event via shared helper.
    await send_to_chat(chat_id, response_text, dedup=dedup, channel=channel)

    return response_text


async def send_to_chat(
    chat_id: str,
    text: str,
    *,
    dedup: DeduplicationService | None = None,
    channel: BaseChannel | None = None,
) -> None:
    """Send a message to a chat with dedup recording and event emission.

    Delegates to ``BaseChannel.send_and_track()`` when a channel is provided,
    which centralizes the send → dedup → event pipeline.  When no channel
    is available (persistence-only paths), handles dedup and event emission
    directly so that outbound tracking remains consistent.

    Callers that only need persistence without an actual channel send can
    pass ``channel=None``.
    """
    if channel:
        await channel.send_and_track(chat_id, text, dedup=dedup)
        return

    # No channel — record dedup and emit event directly.
    if dedup:
        dedup.record_outbound(chat_id, text)

    try:
        await get_event_bus().emit(
            Event(
                name="response_sent",
                data={"chat_id": chat_id, "response_length": len(text)},
                source="response_delivery.send_to_chat",
                correlation_id=get_correlation_id(),
            )
        )
    except Exception:
        log_noncritical(
            NonCriticalCategory.EVENT_EMISSION,
            f"Failed to emit response_sent event for chat {chat_id}",
            logger=log,
        )
