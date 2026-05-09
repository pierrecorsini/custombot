"""
core/command_bus.py — Command bus with middleware pipeline for skill execution.

Routes tool calls through a configurable middleware chain instead of
dispatching directly to skills. Each middleware can intercept, modify,
or short-circuit a command before it reaches the skill.

Built-in middleware:
  - LoggingMiddleware    — structured execution logging
  - AuthMiddleware       — dangerous-skill approval gate
  - RateLimitMiddleware  — per-chat per-skill rate limiting
  - TimeoutMiddleware    — per-skill asyncio timeout enforcement

Usage::

    bus = CommandBus(skill_registry, middlewares=[...])
    result = await bus.execute(Command(
        name="web_search",
        args={"query": "hello"},
        chat_id="chat-123",
    ))
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, Sequence

from src.constants import DEFAULT_SKILL_TIMEOUT

if TYPE_CHECKING:
    from pathlib import Path
    from src.channels.base import SendMediaCallback
    from src.rate_limiter import RateLimiter, RateLimitResult
    from src.skills import SkillRegistry

log = logging.getLogger(__name__)


# ── Data structures ────────────────────────────────────────────────────────


@dataclass(slots=True, frozen=True)
class Command:
    """Immutable command representing a skill execution request."""

    name: str
    args: dict[str, Any] = field(default_factory=dict)
    chat_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandResult:
    """Outcome of a command execution."""

    output: str = ""
    success: bool = True
    error_type: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


# ── Middleware protocol ────────────────────────────────────────────────────


class CommandMiddleware(Protocol):
    """Middleware that can intercept a command before it reaches the skill."""

    async def handle(
        self,
        command: Command,
        next_handler: Callable[[Command], Awaitable[CommandResult]],
    ) -> CommandResult: ...


# ── Built-in middleware ────────────────────────────────────────────────────


class LoggingMiddleware:
    """Log command execution start/end with timing."""

    async def handle(
        self,
        command: Command,
        next_handler: Callable[[Command], Awaitable[CommandResult]],
    ) -> CommandResult:
        log.info(
            "CommandBus ▶ %s [chat: %s]",
            command.name,
            command.chat_id,
            extra={"chat_id": command.chat_id, "skill": command.name},
        )
        result = await next_handler(command)
        status = "✔" if result.success else "✘"
        log.info(
            "CommandBus %s %s [chat: %s]",
            status,
            command.name,
            command.chat_id,
            extra={"chat_id": command.chat_id, "skill": command.name},
        )
        return result


class AuthMiddleware:
    """Gate for dangerous skills requiring human approval."""

    def __init__(self, approval_check: Callable[[str, str, str], Awaitable[bool]]) -> None:
        self._approval_check = approval_check

    async def handle(
        self,
        command: Command,
        next_handler: Callable[[Command], Awaitable[CommandResult]],
    ) -> CommandResult:
        skill = command.metadata.get("_skill")
        if skill is not None and getattr(skill, "dangerous", False):
            args_preview = json.dumps(command.args, default=str)[:200]
            approved = await self._approval_check(
                command.chat_id, command.name, args_preview
            )
            if not approved:
                return CommandResult(
                    output="Execution cancelled — approval was denied or timed out.",
                    success=False,
                    error_type="PermissionError",
                )
        return await next_handler(command)


class RateLimitMiddleware:
    """Per-chat per-skill rate limiting."""

    def __init__(self, rate_limiter: RateLimiter) -> None:
        self._rate_limiter = rate_limiter

    async def handle(
        self,
        command: Command,
        next_handler: Callable[[Command], Awaitable[CommandResult]],
    ) -> CommandResult:
        result: RateLimitResult = self._rate_limiter.check_rate_limit(
            command.chat_id, command.name
        )
        if not result.allowed:
            return CommandResult(
                output=result.message,
                success=False,
                error_type="RateLimitError",
            )
        return await next_handler(command)


class TimeoutMiddleware:
    """Enforce per-skill asyncio timeout."""

    async def handle(
        self,
        command: Command,
        next_handler: Callable[[Command], Awaitable[CommandResult]],
    ) -> CommandResult:
        skill = command.metadata.get("_skill")
        timeout = getattr(skill, "timeout_seconds", DEFAULT_SKILL_TIMEOUT)
        if not isinstance(timeout, (int, float)):
            timeout = DEFAULT_SKILL_TIMEOUT
        try:
            return await asyncio.wait_for(next_handler(command), timeout=timeout)
        except asyncio.TimeoutError:
            return CommandResult(
                output=f"The operation took too long (timeout: {timeout}s).",
                success=False,
                error_type="TimeoutError",
            )


# ── Middleware chain executor ──────────────────────────────────────────────


class _Chain:
    """Linked chain of middleware, similar to MiddlewareChain in message_pipeline."""

    __slots__ = ("_middlewares", "_index", "_terminal")

    def __init__(
        self,
        middlewares: Sequence[CommandMiddleware],
        command: Command,
        terminal: Callable[[Command], Awaitable[CommandResult]],
    ) -> None:
        self._middlewares = middlewares
        self._index = 0
        self._terminal = terminal

    async def __call__(self, command: Command) -> CommandResult:
        if self._index < len(self._middlewares):
            mw = self._middlewares[self._index]
            self._index += 1
            return await mw.handle(command, self)
        return await self._terminal(command)


# ── CommandBus ─────────────────────────────────────────────────────────────


class CommandBus:
    """Route commands through a middleware pipeline then dispatch to skills."""

    def __init__(
        self,
        skill_registry: SkillRegistry,
        middlewares: Sequence[CommandMiddleware] = (),
    ) -> None:
        self._skills = skill_registry
        self._middlewares = list(middlewares)

    async def execute(
        self,
        command: Command,
        *,
        workspace_dir: Path | None = None,
        send_media: SendMediaCallback | None = None,
    ) -> CommandResult:
        """Run *command* through the middleware chain then dispatch."""
        # Attach skill instance to metadata so middleware can inspect it
        skill = self._skills.get(command.name)
        if skill is None:
            return CommandResult(
                output="This tool is not available.",
                success=False,
                error_type="UnknownSkill",
            )

        enriched = Command(
            name=command.name,
            args=command.args,
            chat_id=command.chat_id,
            metadata={**command.metadata, "_skill": skill},
        )

        async def _dispatch(cmd: Command) -> CommandResult:
            try:
                exec_kwargs: dict[str, Any] = dict(cmd.args)
                if send_media is not None:
                    exec_kwargs["send_media"] = send_media
                if workspace_dir is None:
                    return CommandResult(
                        output="No workspace directory provided.",
                        success=False,
                        error_type="ArgumentError",
                    )
                result = await skill.execute(workspace_dir=workspace_dir, **exec_kwargs)
                return CommandResult(output=str(result) if result else "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return CommandResult(
                    output=f"An unexpected error occurred: {exc}",
                    success=False,
                    error_type=type(exc).__name__,
                )

        chain = _Chain(self._middlewares, enriched, _dispatch)
        return await chain(enriched)
