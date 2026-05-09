"""
turn_orchestrator.py — Turn-level orchestration extracted from Bot.

Separates turn preparation, ReAct loop execution, and response delivery
into distinct methods so each stage can be tested independently.

Bot becomes a thin coordinator that delegates to TurnOrchestrator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.bot.context_building import TurnContext
from src.bot.react_loop import StreamCallback
from src.bot.response_delivery import deliver_response
from src.channels.base import IncomingMessage
from src.db.db import ChatMessageParams
from src.core.tool_formatter import ToolLogEntry

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
    from src.core.context_assembler import ContextAssembler
    from src.core.dedup import DeduplicationService
    from src.db.storage_protocol import StorageProvider
    from src.skills import SkillRegistry
    from src.utils.protocols import MemoryProtocol

log = logging.getLogger(__name__)

__all__ = ["TurnOrchestrator"]


@dataclass(slots=True, frozen=True)
class PreparedTurn:
    """Immutable result of turn-preparation."""

    ctx: TurnContext
    workspace_dir: Path
    persistence_failed: bool = False


@dataclass(slots=True, frozen=True)
class DeliveryRequest:
    """Parameter bag for deliver_response."""

    chat_id: str
    raw_response: str
    tool_log: list[ToolLogEntry]
    buffered_persist: list[dict[str, Any]]
    generation: int
    verbose: str
    channel: BaseChannel | None = None
    persistence_failed: bool = False


class TurnOrchestrator:
    """Orchestrates a single message turn: prepare → react → deliver.

    Each method is a self-contained stage that can be tested in isolation.
    The caller (Bot) manages locks, timeouts, and metrics.
    """

    __slots__ = (
        "_db",
        "_memory",
        "_skills",
        "_context_assembler",
        "_dedup",
        "_max_tool_iterations",
        "_stream_response",
    )

    def __init__(
        self,
        *,
        db: StorageProvider,
        memory: MemoryProtocol,
        skills: SkillRegistry,
        context_assembler: ContextAssembler,
        dedup: DeduplicationService,
        max_tool_iterations: int,
        stream_response: bool,
    ) -> None:
        self._db = db
        self._memory = memory
        self._skills = skills
        self._context_assembler = context_assembler
        self._dedup = dedup
        self._max_tool_iterations = max_tool_iterations
        self._stream_response = stream_response

    async def prepare_turn(
        self,
        msg: IncomingMessage,
        turn_ctx: TurnContext | None,
        workspace_dir: Path,
    ) -> PreparedTurn | None:
        """Persist user message and validate routing context.

        Returns None if routing produced no match.
        """
        if turn_ctx is None:
            return None

        try:
            await self._db.upsert_chat_and_save_message(
                ChatMessageParams(
                    chat_id=msg.chat_id,
                    sender_name=msg.sender_name,
                    role="user",
                    content=msg.text,
                    name=msg.sender_name,
                    message_id=msg.message_id,
                )
            )
        except Exception as exc:
            log.error(
                "Failed to persist user turn for chat %s: %s",
                msg.chat_id,
                exc,
                exc_info=True,
                extra={"chat_id": msg.chat_id, "message_id": msg.message_id},
            )
            return PreparedTurn(
                ctx=turn_ctx,
                workspace_dir=workspace_dir,
                persistence_failed=True,
            )

        return PreparedTurn(ctx=turn_ctx, workspace_dir=workspace_dir)

    async def run_react_loop(
        self,
        react_loop_fn: Any,
        chat_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        workspace_dir: Path,
        stream_callback: StreamCallback | None = None,
        channel: BaseChannel | None = None,
        verbose: str = "",
    ) -> tuple[str, list[ToolLogEntry], list[dict[str, Any]]]:
        """Execute the ReAct loop for a single turn.

        Args:
            react_loop_fn: Callable returning the react loop coroutine.
                Accepts a _ReactLoopParams dataclass (from Bot).
        """
        from src.bot._bot import _ReactLoopParams

        stream_cb = stream_callback if verbose == "full" else None
        params = _ReactLoopParams(
            chat_id=chat_id,
            messages=messages,
            tools=tools,
            workspace_dir=workspace_dir,
            stream_callback=stream_cb,
            channel=channel,
        )
        return await react_loop_fn(params)

    async def deliver_response(self, req: DeliveryRequest) -> str | None:
        """Post-ReAct response delivery: format, dedup, persist, emit."""
        return await deliver_response(
            req.chat_id,
            req.raw_response,
            req.tool_log,
            req.buffered_persist,
            req.generation,
            req.verbose,
            context_assembler=self._context_assembler,
            db=self._db,  # type: ignore[arg-type]
            dedup=self._dedup,
            channel=req.channel,
            persistence_failed=req.persistence_failed,
        )
