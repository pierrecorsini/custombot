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
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, Sequence, cast

from src.core.dedup import NullDedupService
from src.core.errors import NonCriticalCategory, log_noncritical
from src.exceptions import format_user_error
from src.logging import get_correlation_id
from src.monitoring.tracing import (
    message_pipeline_span,
    set_correlation_id_on_span,
)
from src.ui.cli_output import log_message_flow

if TYPE_CHECKING:
    from src.bot import Bot
    from src.channels.base import BaseChannel, IncomingMessage
    from src.core.dedup import DeduplicationService
    from src.monitoring import SessionMetrics
    from src.rate_limiter import SlidingWindowTracker
    from src.shutdown import GracefulShutdown

log = logging.getLogger(__name__)

# ── Error-reply rate limiting ─────────────────────────────────────────────

# Max error replies per chat within the window before suppressing.
_ERROR_REPLY_MAX_LIMIT = 5
# Sliding window duration in seconds.
_ERROR_REPLY_WINDOW_SECONDS = 60.0
# Max per-chat trackers retained (LRU eviction beyond this).
_MAX_TRACKED_ERROR_CHATS = 500


# ── Pipeline infrastructure ─────────────────────────────────────────────


@dataclass(slots=True)
class MessageContext:
    """Shared state flowing through the message pipeline."""

    msg: "IncomingMessage"
    op_id: int | None = None
    response: str | None = None


class MessageMiddleware(Protocol):
    """A middleware step in the message processing pipeline."""

    priority: int  # Lower number = higher priority (runs first). Default: 100.

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None: ...


class MiddlewareChain:
    """Named chain executor replacing closure-based ``call_next`` unwinding.

    Unlike anonymous closures, this class appears in tracebacks by name and
    its ``__repr__`` shows which middlewares have executed and which remain,
    making it easy to identify which middleware caused a failure.
    """

    __slots__ = ("_middlewares", "_ctx", "_index")

    def __init__(
        self, middlewares: Sequence[MessageMiddleware], ctx: MessageContext
    ) -> None:
        self._middlewares = middlewares
        self._ctx = ctx
        self._index = 0

    def __repr__(self) -> str:
        names = [type(mw).__name__ for mw in self._middlewares]
        executed = ", ".join(names[: self._index])
        remaining = ", ".join(names[self._index :])
        return f"MiddlewareChain(executed=[{executed}], remaining=[{remaining}])"

    async def call_next(self) -> None:
        """Execute the next middleware in the chain."""
        if self._index < len(self._middlewares):
            mw = self._middlewares[self._index]
            self._index += 1
            await mw(self._ctx, self.call_next)


class MessagePipeline:
    """Composable middleware chain for message processing.

    Middlewares are executed in list order.  Each one receives ``call_next``
    to delegate to the remaining chain.  A middleware that does *not* call
    ``call_next`` short-circuits the pipeline.
    """

    def __init__(self, middlewares: Sequence[MessageMiddleware]) -> None:
        self._middlewares = list(middlewares)

    async def execute(self, ctx: MessageContext) -> None:
        """Run all middlewares in order, wrapped in an OTel span."""
        chain = MiddlewareChain(self._middlewares, ctx)

        async with message_pipeline_span(
            chat_id=ctx.msg.chat_id,
            message_id=ctx.msg.message_id,
            sender_id=ctx.msg.sender_id,
            channel_type=ctx.msg.channel_type,
        ) as span:
            from src.logging import get_correlation_id

            set_correlation_id_on_span(span, get_correlation_id())
            span.set_attribute("messaging.from_me", ctx.msg.fromMe)
            span.set_attribute("messaging.to_me", ctx.msg.toMe)
            await chain.call_next()
            if ctx.response is not None:
                span.set_attribute("custombot.response.length", len(ctx.response))


# ── Config-driven pipeline builder ───────────────────────────────────────


@dataclass(slots=True)
class PipelineDependencies:
    """Dependencies passed to middleware factory functions."""

    shutdown_mgr: "GracefulShutdown"
    session_metrics: "SessionMetrics"
    bot: "Bot"
    channel: "BaseChannel"
    verbose: bool
    dedup: "DeduplicationService" = field(default_factory=NullDedupService)


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

# ── Middleware priority DSL ────────────────────────────────────────────────
#
# Lower number = higher priority (runs first).
# These defaults are assigned to built-in middleware classes above.
# Custom middleware can set a ``priority`` attribute to control ordering.
# Config can override via ``middleware_order: dict[str, int]``.

DEFAULT_PRIORITY = 100

# Built-in recommended priorities (for documentation / config reference).
PRIORITY_ACL = 10
PRIORITY_RATE_LIMIT = 20
PRIORITY_INJECTION = 30
PRIORITY_ERROR_HANDLER = 1000


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
    return ErrorHandlerMiddleware(d.channel, d.session_metrics, verbose=d.verbose, dedup=d.dedup)


def _handle_message_factory(d: PipelineDependencies) -> HandleMessageMiddleware:
    return HandleMessageMiddleware(d.bot, d.channel, dedup=d.dedup)


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
        raise ValueError(f"Invalid middleware path {dotted_path!r}. Use 'module:factory' format.")
    module_path, attr = dotted_path.rsplit(":", 1)
    module = importlib.import_module(module_path)
    obj = getattr(module, attr)
    if not callable(obj):
        raise TypeError(f"{dotted_path!r} is not callable")
    return cast(MiddlewareFactory, obj)


def sort_by_priority(
    middlewares: list[Any],
    custom_priorities: dict[str, int] | None = None,
) -> list[Any]:
    """Sort middleware instances by their ``priority`` attribute.

    Lower priority number → runs first.  Middleware without a ``priority``
    attribute default to ``DEFAULT_PRIORITY`` (100).  ``custom_priorities``
    overrides the priority for middleware whose class name matches a key.

    The sort is stable, so insertion order is preserved for equal priorities.
    """
    overrides = custom_priorities or {}

    def _key(mw: Any) -> int:
        name = type(mw).__name__
        if name in overrides:
            return overrides[name]
        return getattr(mw, "priority", DEFAULT_PRIORITY)

    return sorted(middlewares, key=_key)


def build_pipeline_from_config(
    middleware_order: list[str],
    extra_middleware_paths: list[str],
    deps: PipelineDependencies,
    middleware_priorities: dict[str, int] | None = None,
) -> MessagePipeline:
    """Build a ``MessagePipeline`` from config-driven middleware names and extra paths.

    Args:
        middleware_order: Ordered list of built-in middleware names to include.
        extra_middleware_paths: Dotted import paths for custom middleware factories.
        deps: Shared dependencies passed to every factory.
        middleware_priorities: Optional dict of ``{ClassName: priority}`` to
            override ordering.  When provided, middleware are sorted by
            priority after construction (lower number = runs first).

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

    # Sort by priority when custom priorities are provided or when
    # middleware have non-default priority attributes.
    if middleware_priorities:
        middlewares = sort_by_priority(middlewares, middleware_priorities)

    return MessagePipeline(middlewares)


# ── Middleware implementations ──────────────────────────────────────────


class OperationTrackerMiddleware:
    """Tracks in-flight operations for graceful shutdown."""

    priority = 100  # default

    def __init__(self, shutdown_mgr: GracefulShutdown) -> None:
        self._shutdown_mgr = shutdown_mgr

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        op_id = await self._shutdown_mgr.enter_operation(
            f"message from {ctx.msg.sender_name or ctx.msg.sender_id} in {ctx.msg.chat_id}"
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

    priority = 100

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

    priority = 100

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

    priority = 100

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
                "Message %s rejected by preflight: %s (fromMe=%s, toMe=%s, chat=%s)",
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

    priority = 100

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
    """Catches processing errors, increments error counter, sends user-facing error.

    Uses ``_send_error_reply()`` which delegates to
    ``BaseChannel.send_and_track()`` to centralize send → dedup → event
    so that error-channel traffic is observable in metrics and dedup logs.

    Per-chat rate limiting prevents error-reply amplification: after
    ``_ERROR_REPLY_MAX_LIMIT`` error replies within ``_ERROR_REPLY_WINDOW_SECONDS``
    for a given chat, further replies are suppressed and logged instead of sent.
    """

    priority = 1000

    def __init__(
        self,
        channel: BaseChannel,
        metrics: SessionMetrics,
        verbose: bool = False,
        dedup: DeduplicationService = NullDedupService(),
    ) -> None:
        self._channel = channel
        self._metrics = metrics
        self._verbose = verbose
        self._dedup = dedup
        self._error_reply_trackers: OrderedDict[str, SlidingWindowTracker] = OrderedDict()

    def _check_error_reply_rate(self, chat_id: str) -> bool:
        """Check whether an error reply is allowed for *chat_id*.

        Uses a per-chat ``SlidingWindowTracker`` with LRU eviction to cap
        memory.  Returns ``True`` if the reply may proceed, ``False`` if it
        should be suppressed.
        """
        from src.rate_limiter import SlidingWindowTracker

        tracker = self._error_reply_trackers.get(chat_id)
        if tracker is not None:
            self._error_reply_trackers.move_to_end(chat_id)
        else:
            if len(self._error_reply_trackers) >= _MAX_TRACKED_ERROR_CHATS:
                evicted, _ = self._error_reply_trackers.popitem(last=False)
                log.debug("Evicted error-reply tracker for %s (LRU cap)", evicted)
            tracker = SlidingWindowTracker(
                window_size_seconds=_ERROR_REPLY_WINDOW_SECONDS,
                max_limit=_ERROR_REPLY_MAX_LIMIT,
            )
            self._error_reply_trackers[chat_id] = tracker

        allowed, _, _ = tracker.check_only()
        if allowed:
            tracker.record()
        return allowed

    async def _send_error_reply(self, chat_id: str, text: str) -> None:
        """Send an error reply with dedup recording and event emission.

        Delegates to ``BaseChannel.send_and_track()`` which centralizes the
        send → dedup → event pipeline, ensuring error responses appear in
        dedup logs and event-bus metrics just like normal responses,
        rate-limit warnings, and scheduled replies.
        """
        try:
            await self._channel.send_and_track(chat_id, text, dedup=self._dedup)
        except Exception as send_exc:
            log.warning(
                "Failed to send error message to %s (channel may be disconnected): %s",
                chat_id,
                send_exc,
            )

    @staticmethod
    def _get_correlation_id() -> str | None:
        """Retrieve the current correlation ID for event emission."""
        try:
            from src.logging import get_correlation_id

            return get_correlation_id()
        except Exception:
            return None

    async def __call__(
        self,
        ctx: MessageContext,
        call_next: Callable[[], Awaitable[None]],
    ) -> None:
        try:
            await call_next()
        except asyncio.CancelledError:
            log.info("Message handling cancelled for %s (shutdown?)", ctx.msg.chat_id)
            raise
        except Exception as exc:
            self._metrics.increment_errors()

            corr_id = get_correlation_id()
            error_msg = format_user_error(exc, correlation_id=corr_id)
            log.error("Error handling message: %s", exc, exc_info=self._verbose)

            if self._check_error_reply_rate(ctx.msg.chat_id):
                await self._send_error_reply(ctx.msg.chat_id, error_msg)
            else:
                log.warning(
                    "Error-reply rate limit exceeded for %s — suppressing reply",
                    ctx.msg.chat_id,
                )


class HandleMessageMiddleware:
    """Core message handling: calls bot.handle_message and sends response.

    Uses ``BaseChannel.send_and_track()`` to centralize the send → dedup →
    event pipeline, ensuring normal responses are tracked in dedup logs and
    emit ``response_sent`` events — matching the pattern used by
    ``ErrorHandlerMiddleware``.

    .. rubric:: Architectural note — send ownership

    This middleware owns the send responsibility directly: after
    ``bot.handle_message()`` returns a response, it calls
    ``channel.send_and_track()`` here rather than delegating to
    ``Bot._deliver_response()``.  This is intentional — the two paths
    serve different purposes:

    **Middleware pipeline** (this class):
        Fast-path delivery.  Receives a fully-formed response string from
        ``bot.handle_message()``, sends it via ``send_and_track()``, and
        records outbound dedup + ``response_sent`` event.  Does **not**
        handle persistence, content filtering, or generation-conflict
        resolution — those are the Bot/ReAct path's responsibilities.

    **Bot._deliver_response()** path (``response_delivery.deliver_response``):
        Full post-ReAct delivery pipeline: finalize turn, filter sensitive
        content (``filter_response_content``), dedup, persist messages to
        DB (``save_messages_batch``), handle generation conflicts, then send.

    Keeping the middleware send separate avoids double-persisting or
    double-filtering a response that has already been through the ReAct
    delivery pipeline inside ``handle_message()``.  Future contributors
    should not merge these paths unless ``handle_message()`` is refactored
    to skip its own delivery step when invoked from the middleware.
    """

    priority = 100

    def __init__(
        self,
        bot: Bot,
        channel: BaseChannel,
        dedup: DeduplicationService = NullDedupService(),
    ) -> None:
        self._bot = bot
        self._channel = channel
        self._dedup = dedup

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
            await self._channel.send_and_track(
                ctx.msg.chat_id, response, dedup=self._dedup
            )
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
