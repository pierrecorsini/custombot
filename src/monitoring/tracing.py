"""
src/monitoring/tracing.py — OpenTelemetry-compatible distributed tracing.

Adds OTel spans to the message pipeline, LLM calls, skill execution, and
message delivery.  The ``opentelemetry-api`` / ``opentelemetry-sdk``
packages are **optional** — all public helpers degrade to no-ops when the
packages are not installed, so existing deployments work without changes.

Configuration (environment variables):

    OTEL_TRACES_EXPORTER
        ``console`` (default) — prints spans to stdout.
        ``otlp``            — sends to an OTLP endpoint (requires
                              ``opentelemetry-exporter-otlp``).
        ``none``            — disables tracing entirely.

    OTEL_EXPORTER_OTLP_ENDPOINT
        OTLP collector endpoint (e.g. ``http://localhost:4317``).

    OTEL_SERVICE_NAME
        Service name for spans (default: ``custombot``).

Usage::

    from src.monitoring.tracing import get_tracer, message_pipeline_span

    tracer = get_tracer()

    with tracer.start_as_current_span("my_operation") as span:
        span.set_attribute("key", "value")
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager, contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from opentelemetry.trace import Span as OTelSpan

log = logging.getLogger(__name__)

# ── Availability flag ────────────────────────────────────────────────────

_OTEL_AVAILABLE: bool = False

try:
    from opentelemetry import trace  # noqa: F401
    from opentelemetry.sdk.trace import TracerProvider  # noqa: F401

    _OTEL_AVAILABLE = True
except ImportError:
    pass


def is_tracing_available() -> bool:
    """Return ``True`` when OpenTelemetry packages are installed."""
    return _OTEL_AVAILABLE


# ── No-op fallbacks (used when OTel is not installed) ────────────────────


class _NoOpSpan:
    """Minimal span interface that silently discards all calls."""

    __slots__ = ()

    def set_attribute(self, key: str, value: Any) -> None:  # noqa: ARG002
        pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:  # noqa: ARG002
        pass

    def record_exception(self, exception: BaseException) -> None:  # noqa: ARG002
        pass

    def is_recording(self) -> bool:
        return False

    @property
    def context(self) -> None:
        return None

    def __enter__(self) -> _NoOpSpan:
        return self

    def __exit__(self, *args: object) -> None:
        pass


# Type alias for span annotations.  Type checkers resolve this to the
# ``OTelSpan | _NoOpSpan`` union (via the ``TYPE_CHECKING`` override below);
# at runtime it is simply ``_NoOpSpan`` so the name is always importable.
if TYPE_CHECKING:
    Span = OTelSpan | _NoOpSpan  # type: ignore[assignment]
else:
    Span = _NoOpSpan


class _NoOpTracer:
    """Tracer that returns no-op spans when OTel is not available."""

    __slots__ = ()

    @contextmanager
    def start_as_current_span(
        self,
        name: str,  # noqa: ARG002
        attributes: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> Iterator[_NoOpSpan]:
        yield _NoOpSpan()

    @asynccontextmanager
    async def start_as_current_span_async(
        self,
        name: str,  # noqa: ARG002
        attributes: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> AsyncIterator[_NoOpSpan]:
        yield _NoOpSpan()

    def start_span(
        self,
        name: str,  # noqa: ARG002
        attributes: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> _NoOpSpan:
        return _NoOpSpan()


_noop_tracer = _NoOpTracer()

# ── Singleton tracer ─────────────────────────────────────────────────────

_tracer: Any = None
_initialized: bool = False


def _setup_provider() -> Any:
    """Configure and return an OTel ``Tracer`` (or the no-op fallback).

    Called once on first ``get_tracer()`` invocation.
    """
    global _tracer, _initialized  # noqa: PLW0603
    _initialized = True

    if not _OTEL_AVAILABLE:
        log.debug("OpenTelemetry packages not installed — tracing disabled")
        return _noop_tracer

    exporter_name = os.environ.get("OTEL_TRACES_EXPORTER", "none").lower()

    if exporter_name == "none":
        log.info("OTel tracing explicitly disabled via OTEL_TRACES_EXPORTER=none")
        return _noop_tracer

    try:
        from opentelemetry.sdk.trace import TracerProvider as _TP
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.resources import Resource

        service_name = os.environ.get("OTEL_SERVICE_NAME", "custombot")
        resource = Resource.create({"service.name": service_name})
        provider = _TP(resource=resource)

        if exporter_name == "console":
            from opentelemetry.sdk.trace.export import ConsoleSpanExporter

            processor = BatchSpanProcessor(ConsoleSpanExporter())
            provider.add_span_processor(processor)
            log.info(
                "OTel tracing enabled (console exporter, service=%s)",
                service_name,
            )

        elif exporter_name == "otlp":
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                    OTLPSpanExporter,
                )
            except ImportError:
                try:
                    from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                        OTLPSpanExporter,
                    )
                except ImportError:
                    log.warning(
                        "OTLP exporter requested but opentelemetry-exporter-otlp "
                        "is not installed — falling back to console"
                    )
                    from opentelemetry.sdk.trace.export import ConsoleSpanExporter

                    processor = BatchSpanProcessor(ConsoleSpanExporter())
                    provider.add_span_processor(processor)
                    _tracer = provider.get_tracer("custombot", "1.0.0")
                    return _tracer

            endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
            exporter = OTLPSpanExporter(endpoint=endpoint)
            provider.add_span_processor(BatchSpanProcessor(exporter))
            log.info(
                "OTel tracing enabled (OTLP exporter, endpoint=%s, service=%s)",
                endpoint,
                service_name,
            )
        else:
            log.warning(
                "Unknown OTEL_TRACES_EXPORTER=%r — tracing disabled",
                exporter_name,
            )
            return _noop_tracer

        # Set as global default provider so third-party instrumentations
        # (if installed) pick it up automatically.
        from opentelemetry import trace as _trace

        _trace.set_tracer_provider(provider)

        _tracer = provider.get_tracer("custombot", "1.0.0")
        return _tracer

    except Exception as exc:
        log.warning("Failed to initialise OTel tracing: %s — tracing disabled", exc)
        return _noop_tracer


def get_tracer() -> Any:
    """Return the global OTel tracer (or the no-op fallback)."""
    if not _initialized:
        return _setup_provider()
    return _tracer or _noop_tracer


def reset_tracer() -> None:
    """Reset the global tracer (for testing)."""
    global _tracer, _initialized  # noqa: PLW0603
    _tracer = None
    _initialized = False


# ── Typed span helpers ───────────────────────────────────────────────────


@asynccontextmanager
async def message_pipeline_span(
    chat_id: str,
    message_id: str | None = None,
    sender_id: str | None = None,
    channel_type: str | None = None,
) -> AsyncIterator[Span]:
    """Top-level span covering the full message pipeline execution."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "messaging.system": "custombot",
        "messaging.destination": chat_id,
    }
    if message_id:
        attrs["messaging.message.id"] = message_id
    if sender_id:
        attrs["messaging.source"] = sender_id
    if channel_type:
        attrs["messaging.channel_type"] = channel_type

    if _OTEL_AVAILABLE:
        from opentelemetry import trace

        with tracer.start_as_current_span("message_pipeline", attributes=attrs) as span:
            yield span
    else:
        async with tracer.start_as_current_span_async("message_pipeline", attributes=attrs) as span:
            yield span


@contextmanager
def react_loop_span(
    chat_id: str,
    iteration: int,
    max_iterations: int,
) -> Iterator[Span]:
    """Span for a single ReAct loop iteration."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "custombot.react.iteration": iteration,
        "custombot.react.max_iterations": max_iterations,
        "messaging.destination": chat_id,
    }
    with tracer.start_as_current_span("react_loop", attributes=attrs) as span:
        yield span


@contextmanager
def llm_call_span(
    chat_id: str,
    iteration: int,
    use_streaming: bool,
    tool_count: int | None = None,
) -> Iterator[Span]:
    """Span for an LLM chat-completion call."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "gen_ai.system": "openai",
        "gen_ai.operation.name": "chat",
        "messaging.destination": chat_id,
        "custombot.react.iteration": iteration,
        "custombot.llm.streaming": use_streaming,
    }
    if tool_count is not None:
        attrs["custombot.llm.tool_count"] = tool_count

    with tracer.start_as_current_span("llm.chat_completion", attributes=attrs) as span:
        yield span


@contextmanager
def skill_execution_span(
    skill_name: str,
    chat_id: str,
    args_size_bytes: int | None = None,
) -> Iterator[Span]:
    """Span for a single skill execution."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "custombot.skill.name": skill_name,
        "messaging.destination": chat_id,
    }
    if args_size_bytes is not None:
        attrs["custombot.skill.args_size_bytes"] = args_size_bytes

    with tracer.start_as_current_span("skill.execute", attributes=attrs) as span:
        yield span


@contextmanager
def tool_calls_span(
    chat_id: str,
    call_count: int,
) -> Iterator[Span]:
    """Span for a batch of tool calls within one ReAct iteration."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "custombot.tool_call.count": call_count,
        "messaging.destination": chat_id,
    }
    with tracer.start_as_current_span("tool_calls.process", attributes=attrs) as span:
        yield span


@contextmanager
def context_assembly_span(chat_id: str, rule_id: str | None = None) -> Iterator[Span]:
    """Span for the context-assembly phase."""
    tracer = get_tracer()
    attrs: dict[str, Any] = {"messaging.destination": chat_id}
    if rule_id:
        attrs["custombot.routing.rule_id"] = rule_id

    with tracer.start_as_current_span("context.assembly", attributes=attrs) as span:
        yield span


@contextmanager
def message_receive_span(*, chat_id: str, channel_type: str) -> Iterator[Span]:
    """Span for the message-receive phase of the lifecycle.

    Covers the window from channel adapter ingestion through to hand-off
    to the routing layer.
    """
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "messaging.destination": chat_id,
        "messaging.channel_type": channel_type,
    }
    with tracer.start_as_current_span("message.receive", attributes=attrs) as span:
        yield span


@contextmanager
def routing_match_span(*, sender_id: str, chat_id: str) -> Iterator[Span]:
    """Span for the routing-match phase.

    Covers rule evaluation and selection of the instruction / persona to
    apply for the incoming message.
    """
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "messaging.source": sender_id,
        "messaging.destination": chat_id,
    }
    with tracer.start_as_current_span("routing.match", attributes=attrs) as span:
        yield span


@contextmanager
def response_delivery_span(*, chat_id: str, response_length: int) -> Iterator[Span]:
    """Span for the response-delivery phase.

    Covers sending the final LLM-generated text back through the channel
    adapter to the user.
    """
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "messaging.destination": chat_id,
        "custombot.response.length": response_length,
    }
    with tracer.start_as_current_span("response.delivery", attributes=attrs) as span:
        yield span


@asynccontextmanager
async def db_guarded_write_span(
    operation: str,
    max_retries: int,
    budget_total: float,
) -> AsyncIterator[Span]:
    """Span for a ``Database._guarded_write`` call with retry tracking.

    Attributes set on the span:
        ``custombot.db.operation``          — write operation name
        ``custombot.db.max_retries``        — configured retry limit
        ``custombot.db.retry_budget_total`` — total retry budget in seconds

    Callers update per-attempt attributes (``attempt``, ``delay_seconds``,
    ``budget_remaining``) and record exceptions via ``record_exception_safe``.
    """
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "custombot.db.operation": operation,
        "custombot.db.max_retries": max_retries,
        "custombot.db.retry_budget_total": budget_total,
    }

    if _OTEL_AVAILABLE:
        from opentelemetry import trace

        with tracer.start_as_current_span("db.guarded_write", attributes=attrs) as span:
            yield span
    else:
        async with tracer.start_as_current_span_async("db.guarded_write", attributes=attrs) as span:
            yield span


@contextmanager
def db_retry_attempt_span(
    attempt: int,
) -> Iterator[Span]:
    """Span for a single retry attempt within ``_guarded_write``.

    Each attempt in the retry loop gets its own child span under the
    ``db.guarded_write`` parent span.  Callers set per-attempt attributes
    (``delay_seconds``, ``budget_remaining``) and record exceptions on
    the returned span.
    """
    tracer = get_tracer()
    attrs: dict[str, Any] = {
        "custombot.db.retry.attempt": attempt,
    }
    with tracer.start_as_current_span("db.guarded_write.retry", attributes=attrs) as span:
        yield span


def set_correlation_id_on_span(span: Span, correlation_id: str | None) -> None:
    """Attach the application-level correlation ID to the current span."""
    if correlation_id and span.is_recording():
        span.set_attribute("custombot.correlation_id", correlation_id)


def record_exception_safe(span: Span, exc: BaseException) -> None:
    """Record an exception on the span if OTel is available."""
    if _OTEL_AVAILABLE and span.is_recording():
        span.record_exception(exc)


def add_span_event(
    span: Span, name: str, attributes: dict[str, Any] | None = None
) -> None:
    """Add a structured event to the span (no-op when OTel is unavailable).

    Used to record key milestones in the message lifecycle:
    ``routing_matched``, ``context_assembled``, ``llm_call_started``,
    ``tool_executed``, ``response_delivered``.
    """
    if span.is_recording():
        span.add_event(name, attributes or {})


# ── Health check helper ──────────────────────────────────────────────────


def get_tracing_status() -> dict[str, Any]:
    """Return tracing status for the health endpoint."""
    return {
        "available": _OTEL_AVAILABLE,
        "initialized": _initialized,
        "exporter": os.environ.get(
            "OTEL_TRACES_EXPORTER", "console" if _OTEL_AVAILABLE else "unavailable"
        ),
        "service_name": os.environ.get("OTEL_SERVICE_NAME", "custombot"),
    }
