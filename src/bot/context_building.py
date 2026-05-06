"""
context_building.py — Build LLM turn context from routing match + instruction.

Extracts the context-assembly stage (routing match, instruction loading,
context assembler invocation) from :class:`Bot` so it can be unit-tested
independently of the full ReAct loop.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.channels.base import IncomingMessage
from src.core.context_builder import ChatMessage
from src.core.event_bus import Event, get_event_bus
from src.logging import get_correlation_id
from src.monitoring.tracing import (
    context_assembly_span,
    set_correlation_id_on_span,
)
from src.routing import MatchingContext

if TYPE_CHECKING:
    from src.core.context_assembler import ContextAssembler
    from src.core.instruction_loader import InstructionLoader
    from src.routing import RoutingEngine


log = logging.getLogger(__name__)

__all__ = ["TurnContext", "build_turn_context", "routing_show_errors_var"]

# Per-request routing flag — contextvar prevents cross-request state leaks
# when multiple messages are processed concurrently on the event loop.
routing_show_errors_var: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "routing_show_errors", default=True
)


@dataclass(slots=True, frozen=True)
class TurnContext:
    """Immutable context assembled for a single ReAct turn.

    Built by :func:`build_turn_context` from routing match, instruction
    loading, memory reads, and the LLM message list.  Returned as a single
    object so the context-assembly stage can be unit-tested independently of
    the full ReAct loop.
    """

    messages: list[ChatMessage]
    rule_id: str
    skill_exec_verbose: str
    show_errors: bool


async def build_turn_context(
    msg: IncomingMessage,
    *,
    routing: RoutingEngine,
    instruction_loader: InstructionLoader,
    context_assembler: ContextAssembler,
    channel: BaseChannel | None = None,
) -> TurnContext | None:
    """Match routing rule, load instruction, and assemble LLM messages.

    Returns ``None`` when routing produces no match (message dropped).
    """
    if not routing.has_rules:
        log.warning(
            "Routing engine has no rules loaded — message from %s in chat %s ignored. "
            "Ensure workspace/instructions/ contains at least a 'chat.agent.md' "
            "with routing frontmatter.",
            msg.sender_id,
            msg.chat_id,
        )
        await get_event_bus().emit(
            Event(
                name="message_dropped",
                data={"chat_id": msg.chat_id, "sender_id": msg.sender_id, "reason": "no_rules"},
                source="context_building.build_turn_context",
                correlation_id=get_correlation_id(),
            )
        )
        return None

    match_ctx = MatchingContext.from_message(msg)
    matched_rule, instruction_filename = await routing.match_with_rule(msg, ctx=match_ctx)
    if not matched_rule:
        log.info(
            "No routing rule matched for message from %s (fromMe=%s, toMe=%s), ignoring",
            msg.sender_id,
            msg.fromMe,
            msg.toMe,
        )
        await get_event_bus().emit(
            Event(
                name="message_dropped",
                data={"chat_id": msg.chat_id, "sender_id": msg.sender_id, "reason": "no_match"},
                source="context_building.build_turn_context",
                correlation_id=get_correlation_id(),
            )
        )
        return None

    routing_show_errors_var.set(matched_rule.showErrors)

    log.info(
        "Matched routing rule '%s' (instruction: %s) for message from %s",
        matched_rule.id,
        instruction_filename,
        msg.sender_id,
    )

    instruction_content = instruction_loader.load(instruction_filename or "default.md")
    channel_prompt = channel.get_channel_prompt() if channel else None

    with context_assembly_span(chat_id=msg.chat_id, rule_id=matched_rule.id) as span:
        set_correlation_id_on_span(span, get_correlation_id())
        result = await context_assembler.assemble(
            chat_id=msg.chat_id,
            channel_prompt=channel_prompt,
            instruction=instruction_content,
            rule_id=matched_rule.id,
        )
        if result is not None:
            span.set_attribute("custombot.context.message_count", len(result.messages))

    if result is None:
        log.warning(
            "Context assembly returned None for chat %s — build_context failure",
            msg.chat_id,
            extra={"chat_id": msg.chat_id},
        )
        return None

    return TurnContext(
        messages=result.messages,
        rule_id=result.rule_id or matched_rule.id,
        skill_exec_verbose=matched_rule.skillExecVerbose,
        show_errors=matched_rule.showErrors,
    )
