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

from src.bot._event_helpers import _emit_error_event_safe, _emit_event_safe
from src.core.dedup import NullDedupService, outbound_key
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import EVENT_GENERATION_CONFLICT
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

# Maximum retry attempts for generation-conflict recovery (re-read + merge).
_MAX_CONFLICT_RETRIES = 3


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
    dedup: DeduplicationService = NullDedupService(),
    channel: BaseChannel | None = None,
    persistence_failed: bool = False,
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

    When *persistence_failed* is True the user-message persist in
    ``_prepare_turn`` failed, so assistant persistence is also skipped to
    avoid inconsistent conversation state.
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
    # Use keyed variant so the xxh64 hash is computed once and reused
    # by record_outbound_keyed() after successful send.
    is_dup, dedup_key = dedup.check_outbound_with_key(chat_id, response_text)
    if is_dup:
        log.info(
            "Outbound dedup suppressed duplicate response for chat %s",
            chat_id,
            extra={"chat_id": chat_id},
        )
        return None

    if persistence_failed:
        # User-message persistence failed in _prepare_turn — skip assistant
        # persistence too so conversation state stays consistent on restart.
        log.warning(
            "Skipping assistant persistence for chat %s because "
            "user-message persist failed earlier.",
            chat_id,
            extra={"chat_id": chat_id},
        )
    else:
        batch = [*buffered_persist, {"role": "assistant", "content": response_text}]
        for _attempt in range(_MAX_CONFLICT_RETRIES):
            if db.check_generation(chat_id, generation):
                break  # No conflict — proceed with write

            # Generation conflict: re-read latest state and merge.
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
                await send_to_chat(chat_id, response_text, dedup=dedup, channel=channel, dedup_key=dedup_key)
                return response_text

            log.warning(
                "Write conflict for chat %s (attempt %d/%d) — generation "
                "changed during processing. Re-read %d existing messages, "
                "merged batch from %d to %d entries.",
                chat_id,
                _attempt + 1,
                _MAX_CONFLICT_RETRIES,
                len(existing),
                original_size,
                len(batch),
                extra={"chat_id": chat_id},
            )
            await _emit_event_safe(
                EVENT_GENERATION_CONFLICT,
                {
                    "chat_id": chat_id,
                    "expected_generation": generation,
                    "current_generation": current_gen,
                    "existing_count": len(existing),
                    "merged_batch_size": len(batch),
                    "attempt": _attempt + 1,
                },
                "response_delivery.deliver_response",
                get_correlation_id(),
            )
            generation = current_gen
        else:
            log.error(
                "Generation conflict retries exhausted for chat %s after "
                "%d attempts — proceeding with write as best effort",
                chat_id,
                _MAX_CONFLICT_RETRIES,
                extra={"chat_id": chat_id},
            )
            await _emit_event_safe(
                EVENT_GENERATION_CONFLICT,
                {
                    "chat_id": chat_id,
                    "retries_exhausted": True,
                },
                "response_delivery.deliver_response",
                get_correlation_id(),
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
            await _emit_error_event_safe(
                exc,
                "response_delivery.deliver_response",
                extra_data={
                    "chat_id": chat_id,
                    "source": "response_delivery.deliver_response.save_messages_batch",
                },
            )

    # Record outbound dedup + emit response_sent event via shared helper.
    await send_to_chat(chat_id, response_text, dedup=dedup, channel=channel, dedup_key=dedup_key)

    return response_text


async def send_to_chat(
    chat_id: str,
    text: str,
    *,
    dedup: DeduplicationService = NullDedupService(),
    channel: BaseChannel | None = None,
    dedup_key: str | None = None,
) -> None:
    """Send a message to a chat with dedup recording and event emission.

    Delegates to ``BaseChannel.send_and_track()`` when a channel is provided,
    which centralizes the send → dedup → event pipeline.  When no channel
    is available (persistence-only paths), handles dedup and event emission
    directly so that outbound tracking remains consistent.

    Callers that only need persistence without an actual channel send can
    pass ``channel=None``.

    When *dedup_key* is provided (from :meth:`check_outbound_with_key`),
    the pre-computed xxh64 hash is reused via :meth:`record_outbound_keyed`.
    When absent, the hash is computed inline via :func:`outbound_key` so no
    redundant xxh64 computation occurs.
    """
    if channel:
        await channel.send_and_track(chat_id, text, dedup=dedup, dedup_key=dedup_key)
        return

    # No channel — record dedup and emit event directly.
    if dedup_key is not None:
        dedup.record_outbound_keyed(dedup_key)
    else:
        # Compute hash inline to avoid double-hash via record_outbound().
        dedup.record_outbound_keyed(outbound_key(chat_id, text))

    await _emit_event_safe(
        "response_sent",
        {"chat_id": chat_id, "response_length": len(text)},
        "response_delivery.send_to_chat",
        get_correlation_id(),
    )
