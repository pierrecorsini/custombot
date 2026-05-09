"""src/monitoring/otel_metrics.py — OpenTelemetry metrics instruments.

Provides counters, histograms, and gauges for key application metrics.
Falls back to no-op implementations when ``opentelemetry-api`` is not
installed, so the application runs without the dependency in development.

Usage::

    from src.monitoring.otel_metrics import get_meter, metrics

    meter = get_meter()
    metrics.llm_requests.add(1, {"model": "gpt-4o"})
    metrics.message_latency.record(1.5, {"chat_id": "123"})
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# Optional OpenTelemetry import — graceful degradation.
_HAS_OTEL = False
try:
    from opentelemetry import metrics as otel_metrics_api
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.resources import Resource

    _HAS_OTEL = True
except ImportError:
    pass


# ── No-op fallbacks ──────────────────────────────────────────────────────


class _NoOpCounter:
    """No-op counter when OpenTelemetry is not installed."""

    def add(self, amount: int | float, attributes: dict[str, Any] | None = None) -> None:
        pass


class _NoOpHistogram:
    """No-op histogram when OpenTelemetry is not installed."""

    def record(self, value: int | float, attributes: dict[str, Any] | None = None) -> None:
        pass


class _NoOpUpDownCounter:
    """No-op up-down counter when OpenTelemetry is not installed."""

    def add(self, amount: int | float, attributes: dict[str, Any] | None = None) -> None:
        pass


class _NoOpGauge:
    """No-op gauge when OpenTelemetry is not installed."""

    def set(self, value: int | float, attributes: dict[str, Any] | None = None) -> None:
        pass


class _NoOpMeter:
    """No-op meter when OpenTelemetry is not installed."""

    def create_counter(self, name: str, **kwargs: Any) -> _NoOpCounter:
        return _NoOpCounter()

    def create_histogram(self, name: str, **kwargs: Any) -> _NoOpHistogram:
        return _NoOpHistogram()

    def create_up_down_counter(self, name: str, **kwargs: Any) -> _NoOpUpDownCounter:
        return _NoOpUpDownCounter()

    def create_gauge(self, name: str, **kwargs: Any) -> _NoOpGauge:
        return _NoOpGauge()


# ── Application Metrics ──────────────────────────────────────────────────


class ApplicationMetrics:
    """Typed access to all OpenTelemetry instruments.

    All instruments are created lazily via the meter.  When
    OpenTelemetry is not installed, no-op instruments are used
    so all calls are safe no-ops with zero overhead.
    """

    __slots__ = (
        "llm_requests",
        "llm_latency",
        "llm_tokens_prompt",
        "llm_tokens_completion",
        "llm_errors",
        "messages_received",
        "messages_processed",
        "messages_rejected",
        "message_latency",
        "tool_executions",
        "tool_latency",
        "tool_errors",
        "db_writes",
        "db_write_latency",
        "db_write_retries",
        "dedup_inbound_hits",
        "dedup_outbound_hits",
        "active_chats",
        "routing_latency",
        "routing_matches",
        "routing_misses",
        "skill_error_rate",
    )

    def __init__(self, meter: _NoOpMeter | Any) -> None:
        self.llm_requests = meter.create_counter(
            "custombot.llm.requests",
            description="Total LLM API requests",
            unit="1",
        )
        self.llm_latency = meter.create_histogram(
            "custombot.llm.latency",
            description="LLM API request latency",
            unit="s",
        )
        self.llm_tokens_prompt = meter.create_counter(
            "custombot.llm.tokens.prompt",
            description="Total prompt tokens consumed",
            unit="1",
        )
        self.llm_tokens_completion = meter.create_counter(
            "custombot.llm.tokens.completion",
            description="Total completion tokens produced",
            unit="1",
        )
        self.llm_errors = meter.create_counter(
            "custombot.llm.errors",
            description="Total LLM errors",
            unit="1",
        )
        self.messages_received = meter.create_counter(
            "custombot.messages.received",
            description="Total messages received from channels",
            unit="1",
        )
        self.messages_processed = meter.create_counter(
            "custombot.messages.processed",
            description="Total messages successfully processed",
            unit="1",
        )
        self.messages_rejected = meter.create_counter(
            "custombot.messages.rejected",
            description="Total messages rejected (rate limit, dedup, ACL)",
            unit="1",
        )
        self.message_latency = meter.create_histogram(
            "custombot.messages.latency",
            description="End-to-end message processing latency",
            unit="s",
        )
        self.tool_executions = meter.create_counter(
            "custombot.tool.executions",
            description="Total tool/skill executions",
            unit="1",
        )
        self.tool_latency = meter.create_histogram(
            "custombot.tool.latency",
            description="Tool execution latency",
            unit="s",
        )
        self.db_writes = meter.create_counter(
            "custombot.db.writes",
            description="Total database write operations",
            unit="1",
        )
        self.db_write_latency = meter.create_histogram(
            "custombot.db.write_latency",
            description="Database write operation latency",
            unit="s",
        )
        self.db_write_retries = meter.create_counter(
            "custombot.db.write_retries",
            description="Total database write retry attempts",
            unit="1",
        )
        self.dedup_inbound_hits = meter.create_counter(
            "custombot.dedup.inbound_hits",
            description="Inbound dedup cache hits",
            unit="1",
        )
        self.dedup_outbound_hits = meter.create_counter(
            "custombot.dedup.outbound_hits",
            description="Outbound dedup cache hits",
            unit="1",
        )
        self.active_chats = meter.create_up_down_counter(
            "custombot.chats.active",
            description="Currently active chat locks",
            unit="1",
        )
        self.tool_errors = meter.create_counter(
            "custombot.tool.errors",
            description="Total tool/skill execution errors",
            unit="1",
        )
        self.routing_latency = meter.create_histogram(
            "custombot.routing.latency",
            description="Routing engine match latency",
            unit="s",
        )
        self.routing_matches = meter.create_counter(
            "custombot.routing.matches",
            description="Total routing matches (rule found)",
            unit="1",
        )
        self.routing_misses = meter.create_counter(
            "custombot.routing.misses",
            description="Total routing misses (no rule matched)",
            unit="1",
        )
        self.skill_error_rate = meter.create_gauge(
            "custombot.skill.error_rate",
            description="Per-skill sliding-window error rate",
            unit="1",
        )


# ── Singleton ────────────────────────────────────────────────────────────

_meter: Any = None
_metrics: ApplicationMetrics | None = None


def get_meter() -> Any:
    """Return the global OpenTelemetry meter (or no-op fallback)."""
    global _meter
    if _meter is None:
        if _HAS_OTEL:
            try:
                resource = Resource.create({"service.name": "custombot"})
                provider = MeterProvider(resource=resource)
                otel_metrics_api.set_meter_provider(provider)
                _meter = otel_metrics_api.get_meter("custombot", "1.0.0")
                log.info("OpenTelemetry metrics initialized")
            except Exception as exc:
                log.warning("Failed to initialize OpenTelemetry: %s", exc)
                _meter = _NoOpMeter()
        else:
            _meter = _NoOpMeter()
    return _meter


def get_metrics() -> ApplicationMetrics:
    """Return the global ApplicationMetrics instance."""
    global _metrics
    if _metrics is None:
        _metrics = ApplicationMetrics(get_meter())
    return _metrics


def reset_metrics() -> None:
    """Reset the global metrics singleton (for testing)."""
    global _meter, _metrics
    _meter = None
    _metrics = None
