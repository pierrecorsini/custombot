"""
src/core/message_pipeline.py — Composable middleware chain for message handling.

Each concern (shutdown tracking, metrics, logging, preflight, typing,
error handling, message handling) is a discrete middleware that can be
composed, reordered, and tested independently.

The pipeline is dynamically extensible via config: built-in middlewares
are referenced by name, and custom middlewares can be added via dotted
import paths (``"module.path:factory_func"``).

Usage::

    from src.core.message_pipeline import build_pipeline_from_config, PipelineDependencies

    deps = PipelineDependencies(
        shutdown_mgr=shutdown_mgr,
        session_metrics=session_metrics,
        bot=bot,
        channel=channel,
        verbose=False,
    )
    pipeline = build_pipeline_from_config(
        middleware_order=["operation_tracker", "metrics", ..., "handle_message"],
        extra_middleware_paths=[],
        deps=deps,
    )
    await pipeline.execute(MessageContext(msg=msg))
"""

from __future__ import annotations

import asyncio
import importlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, Sequence

from src.core.errors import NonCriticalCategory, log_noncritical
from src.ui.cli_output import log_message_flow

if TYPE_CHECKING:
    from src.bot import Bot
    from src.channels.base import BaseChannel
    from src.monitoring import SessionMetrics
    from src.shutdown import GracefulShutdown

log = logging.getLogger(__name__)


# ── Pipeline infrastructure ─────────────────────────────────────────────


@dataclass(slots=True)
class MessageContext:
    """Shared state flowing through the message pipeline."""

    msg: "IncomingMessage"
    op_id: int | None = None
    response: str | None = None


class MessageMiddleware(Protocol):
    """A middleware step in the message processing pipeline."""

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None: ...


class MessagePipeline:
    """Composable middleware chain for message processing.

    Middlewares are executed in list order.  Each one receives ``call_next``
    to delegate to the remaining chain.  A middleware that does *not* call
    ``call_next`` short-circuits the pipeline.
    """

    def __init__(self, middlewares: Sequence[MessageMiddleware]) -> None:
        self._middlewares = list(middlewares)

    async def execute(self, ctx: MessageContext) -> None:
        """Run all middlewares in order."""
        index = 0

        async def call_next() -> None:
            nonlocal index
            if index < len(self._middlewares):
                mw = self._middlewares[index]
                index += 1
                await mw(ctx, call_next)

        await call_next()


# ── Config-driven pipeline builder ───────────────────────────────────────


@dataclass(slots=True)
class PipelineDependencies:
    """Dependencies passed to middleware factory functions."""

    shutdown_mgr: "GracefulShutdown"
    session_metrics: "SessionMetrics"
    bot: "Bot"
    channel: "BaseChannel"
    verbose: bool


MiddlewareFactory = Callable[[PipelineDependencies], Any]

DEFAULT_MIDDLEWARE_ORDER: list[str] = [
    "operation_tracker",
    "metrics",
    "inbound_logging",
    "preflight",
    "typing",
    "error_handler",
    "handle_message",
]


def _operation_tracker_factory(d: PipelineDependencies) -> OperationTrackerMiddleware:
    return OperationTrackerMiddleware(d.shutdown_mgr)


def _metrics_factory(d: PipelineDependencies) -> MetricsMiddleware:
    return MetricsMiddleware(d.session_metrics)


def _inbound_logging_factory(d: PipelineDependencies) -> InboundLoggingMiddleware:
    return InboundLoggingMiddleware()


def _preflight_factory(d: PipelineDependencies) -> PreflightMiddleware:
    return PreflightMiddleware(d.bot)


def _typing_factory(d: PipelineDependencies) -> TypingMiddleware:
    return TypingMiddleware(d.channel)


def _error_handler_factory(d: PipelineDependencies) -> ErrorHandlerMiddleware:
    return ErrorHandlerMiddleware(d.channel, d.session_metrics, verbose=d.verbose)


def _handle_message_factory(d: PipelineDependencies) -> HandleMessageMiddleware:
    return HandleMessageMiddleware(d.bot, d.channel)


BUILTIN_MIDDLEWARE_FACTORIES: dict[str, MiddlewareFactory] = {
    "operation_tracker": _operation_tracker_factory,
    "metrics": _metrics_factory,
    "inbound_logging": _inbound_logging_factory,
    "preflight": _preflight_factory,
    "typing": _typing_factory,
    "error_handler": _error_handler_factory,
    "handle_message": _handle_message_factory,
}


def _import_factory(dotted_path: str) -> MiddlewareFactory:
    """Import a middleware factory from a ``'module.path:callable'`` string."""
    if ":" not in dotted_path:
        raise ValueError(
            f"Invalid middleware path {dotted_path!r}. Use 'module:factory' format."
        )
    module_path, attr = dotted_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    obj = getattr(module, attr)
    if not callable(obj):
        raise TypeError(f"{dotted_path!r} is not callable")
    return obj


def build_pipeline_from_config(
    middleware_order: list[str],
    extra_middleware_paths: list[str],
    deps: PipelineDependencies,
) -> MessagePipeline:
    """Build a ``MessagePipeline`` from config-driven middleware names and extra paths.

    Args:
        middleware_order: Ordered list of built-in middleware names to include.
        extra_middleware_paths: Dotted import paths for custom middleware factories.
        deps: Shared dependencies passed to every factory.

    Returns:
        A fully constructed ``MessagePipeline``.
    """
    middlewares: list[Any] = []

    # Fall back to default order when config doesn't specify one
    order = middleware_order or DEFAULT_MIDDLEWARE_ORDER

    for name in order:
        factory = BUILTIN_MIDDLEWARE_FACTORIES.get(name)
        if factory is None:
            log.warning("Unknown built-in middleware %r — skipping", name)
            continue
        middlewares.append(factory(deps))

    for path in extra_middleware_paths:
        try:
            factory = _import_factory(path)
            middlewares.append(factory(deps))
        except Exception:
            log_noncritical(
                NonCriticalCategory.MIDDLEWARE_LOADING,
                "Failed to load middleware from %r — skipping",
                logger=log,
                extra={"path": path},
            )

    if not middlewares:
        log.warning("No middleware loaded — pipeline will be a no-op")

    return MessagePipeline(middlewares)


# ── Middleware implementations ──────────────────────────────────────────


class OperationTrackerMiddleware:
    """Tracks in-flight operations for graceful shutdown."""

    def __init__(self, shutdown_mgr: GracefulShutdown) -> None:
        self._shutdown_mgr = shutdown_mgr

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        op_id = await self._shutdown_mgr.enter_operation(
            f"message from {ctx.msg.sender_name or ctx.msg.sender_id} "
            f"in {ctx.msg.chat_id}"
        )
        if op_id is None:
            return
        ctx.op_id = op_id
        try:
            await call_next()
        finally:
            await self._shutdown_mgr.exit_operation(op_id)


class MetricsMiddleware:
    """Tracks message count in session metrics."""

    def __init__(self, metrics: SessionMetrics) -> None:
        self._metrics = metrics

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        self._metrics.increment_messages()
        await call_next()


class InboundLoggingMiddleware:
    """Logs incoming message flow."""

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        log_message_flow(
            direction="IN",
            channel=ctx.msg.channel_type or "unknown",
            source=ctx.msg.sender_name or ctx.msg.sender_id,
            destination=ctx.msg.chat_id,
            text=ctx.msg.text,
            from_me=ctx.msg.fromMe,
            to_me=ctx.msg.toMe,
        )
        await call_next()


class PreflightMiddleware:
    """Runs preflight check; short-circuits if message is rejected."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        preflight = await self._bot.preflight_check(ctx.msg)
        if not preflight:
            log.debug(
                "Message %s rejected by preflight: %s "
                "(fromMe=%s, toMe=%s, chat=%s)",
                ctx.msg.message_id,
                preflight.reason,
                ctx.msg.fromMe,
                ctx.msg.toMe,
                ctx.msg.chat_id,
            )
            return
        await call_next()


class TypingMiddleware:
    """Sends typing indicator before processing."""

    def __init__(self, channel: BaseChannel) -> None:
        self._channel = channel

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        await self._channel.send_typing(ctx.msg.chat_id)
        await call_next()


class ErrorHandlerMiddleware:
    """Catches processing errors, increments error counter, sends user-facing error."""

    def __init__(
        self,
        channel: BaseChannel,
        metrics: SessionMetrics,
        verbose: bool = False,
    ) -> None:
        self._channel = channel
        self._metrics = metrics
        self._verbose = verbose

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await call_next()
        except asyncio.CancelledError:
            log.info(
                "Message handling cancelled for %s (shutdown?)", ctx.msg.chat_id
            )
            raise
        except Exception as exc:
            self._metrics.increment_errors()
            from src.exceptions import format_user_error
            from src.logging import get_correlation_id

            corr_id = get_correlation_id()
            error_msg = format_user_error(exc, correlation_id=corr_id)
            log.error(
                "Error handling message: %s", exc, exc_info=self._verbose
            )
            try:
                await self._channel.send_message(ctx.msg.chat_id, error_msg)
            except Exception as send_exc:
                log.warning(
                    "Failed to send error message to %s "
                    "(channel may be disconnected): %s",
                    ctx.msg.chat_id,
                    send_exc,
                )


class HandleMessageMiddleware:
    """Core message handling: calls bot.handle_message and sends response."""

    def __init__(self, bot: Bot, channel: BaseChannel) -> None:
        self._bot = bot
        self._channel = channel

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        async def _stream_tool_update(text: str) -> None:
            try:
                await self._channel.send_message(ctx.msg.chat_id, text)
                await asyncio.sleep(0.5)
            except Exception as exc:
                log.warning(
                    "Failed to stream tool update to %s: %s",
                    ctx.msg.chat_id,
                    exc,
                )

        response = await self._bot.handle_message(
            ctx.msg, channel=self._channel, stream_callback=_stream_tool_update
        )
        if response:
            await self._channel.send_message(ctx.msg.chat_id, response)
            log_message_flow(
                direction="OUT",
                channel=ctx.msg.channel_type or "unknown",
                source="Bot",
                destination=ctx.msg.chat_id,
                text=response,
                from_me=True,
                to_me=False,
            )
        ctx.response = response
