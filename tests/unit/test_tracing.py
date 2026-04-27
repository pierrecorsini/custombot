"""
test_tracing.py — Tests for src/monitoring/tracing.py.

Covers:
- No-op fallbacks when OTel packages are not installed
- Span helper factories (message_pipeline, react_loop, llm_call, etc.)
- Correlation ID attachment and exception recording
- Tracer singleton lifecycle (get_tracer, reset_tracer)
- Health-check status reporting
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

import src.monitoring.tracing as tracing_mod
from src.monitoring.tracing import (
    _NoOpSpan,
    _NoOpTracer,
    context_assembly_span,
    get_tracing_status,
    get_tracer,
    is_tracing_available,
    llm_call_span,
    message_pipeline_span,
    react_loop_span,
    record_exception_safe,
    reset_tracer,
    set_correlation_id_on_span,
    skill_execution_span,
    tool_calls_span,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_tracer_state():
    """Reset the tracing singleton between tests."""
    reset_tracer()
    yield
    reset_tracer()


# ── NoOpSpan tests ──────────────────────────────────────────────────────────


class TestNoOpSpan:
    """Verify _NoOpSpan silently discards all calls."""

    def test_set_attribute_is_noop(self):
        span = _NoOpSpan()
        span.set_attribute("key", "value")  # should not raise

    def test_add_event_is_noop(self):
        span = _NoOpSpan()
        span.add_event("event_name", {"k": "v"})  # should not raise

    def test_record_exception_is_noop(self):
        span = _NoOpSpan()
        span.record_exception(RuntimeError("boom"))  # should not raise

    def test_is_recording_returns_false(self):
        span = _NoOpSpan()
        assert span.is_recording() is False

    def test_context_returns_none(self):
        span = _NoOpSpan()
        assert span.context is None

    def test_context_manager_protocol(self):
        span = _NoOpSpan()
        with span as s:
            assert s is span


# ── NoOpTracer tests ────────────────────────────────────────────────────────


class TestNoOpTracer:
    """Verify _NoOpTracer yields no-op spans."""

    def test_sync_span(self):
        tracer = _NoOpTracer()
        with tracer.start_as_current_span("test") as span:
            assert isinstance(span, _NoOpSpan)

    async def test_async_span(self):
        tracer = _NoOpTracer()
        async with tracer.start_as_current_span_async("test") as span:
            assert isinstance(span, _NoOpSpan)

    def test_start_span_returns_noop(self):
        tracer = _NoOpTracer()
        span = tracer.start_span("test")
        assert isinstance(span, _NoOpSpan)


# ── Span helper tests (no-op path) ──────────────────────────────────────────


class TestMessagePipelineSpan:
    """Test message_pipeline_span async context manager."""

    async def test_yields_span_with_noop_tracer(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            async with message_pipeline_span(
                chat_id="chat-1",
                message_id="msg-1",
                sender_id="user-1",
                channel_type="whatsapp",
            ) as span:
                assert isinstance(span, _NoOpSpan)

    async def test_minimal_args(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            async with message_pipeline_span(chat_id="chat-1") as span:
                assert isinstance(span, _NoOpSpan)


class TestReactLoopSpan:
    """Test react_loop_span sync context manager."""

    def test_yields_span_with_noop_tracer(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with react_loop_span("chat-1", iteration=1, max_iterations=5) as span:
                assert isinstance(span, _NoOpSpan)


class TestLLMCallSpan:
    """Test llm_call_span sync context manager."""

    def test_yields_span_with_noop_tracer(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with llm_call_span(
                chat_id="chat-1",
                iteration=0,
                use_streaming=True,
                tool_count=3,
            ) as span:
                assert isinstance(span, _NoOpSpan)

    def test_without_tool_count(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with llm_call_span(
                chat_id="chat-1", iteration=0, use_streaming=False,
            ) as span:
                assert isinstance(span, _NoOpSpan)


class TestSkillExecutionSpan:
    """Test skill_execution_span sync context manager."""

    def test_yields_span_with_noop_tracer(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with skill_execution_span(
                skill_name="web_search",
                chat_id="chat-1",
                args_size_bytes=128,
            ) as span:
                assert isinstance(span, _NoOpSpan)

    def test_without_args_size(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with skill_execution_span(
                skill_name="echo", chat_id="chat-1",
            ) as span:
                assert isinstance(span, _NoOpSpan)


class TestToolCallsSpan:
    """Test tool_calls_span sync context manager."""

    def test_yields_span_with_noop_tracer(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with tool_calls_span(chat_id="chat-1", call_count=3) as span:
                assert isinstance(span, _NoOpSpan)


class TestContextAssemblySpan:
    """Test context_assembly_span sync context manager."""

    def test_yields_span_with_noop_tracer(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with context_assembly_span(chat_id="chat-1", rule_id="rule-1") as span:
                assert isinstance(span, _NoOpSpan)

    def test_without_rule_id(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            with context_assembly_span(chat_id="chat-1") as span:
                assert isinstance(span, _NoOpSpan)


# ── Utility function tests ──────────────────────────────────────────────────


class TestSetCorrelationIdOnSpan:
    """Test set_correlation_id_on_span helper."""

    def test_noop_span_is_safe(self):
        span = _NoOpSpan()
        set_correlation_id_on_span(span, "corr-123")  # should not raise

    def test_none_correlation_id_is_noop(self):
        span = _NoOpSpan()
        set_correlation_id_on_span(span, None)  # should not raise

    def test_recording_span_sets_attribute(self):
        span = MagicMock()
        span.is_recording.return_value = True
        set_correlation_id_on_span(span, "corr-abc")
        span.set_attribute.assert_called_once_with(
            "custombot.correlation_id", "corr-abc"
        )

    def test_non_recording_span_skips(self):
        span = MagicMock()
        span.is_recording.return_value = False
        set_correlation_id_on_span(span, "corr-abc")
        span.set_attribute.assert_not_called()


class TestRecordExceptionSafe:
    """Test record_exception_safe helper."""

    def test_noop_span_is_safe(self):
        span = _NoOpSpan()
        record_exception_safe(span, RuntimeError("boom"))  # should not raise

    def test_recording_span_when_otel_available(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", True):
            span = MagicMock()
            span.is_recording.return_value = True
            exc = RuntimeError("test error")
            record_exception_safe(span, exc)
            span.record_exception.assert_called_once_with(exc)

    def test_non_recording_span_skips(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", True):
            span = MagicMock()
            span.is_recording.return_value = False
            record_exception_safe(span, RuntimeError("test"))
            span.record_exception.assert_not_called()

    def test_otel_unavailable_skips(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            span = MagicMock()
            span.is_recording.return_value = True
            record_exception_safe(span, RuntimeError("test"))
            span.record_exception.assert_not_called()


# ── Tracer lifecycle tests ──────────────────────────────────────────────────


class TestGetTracer:
    """Test singleton tracer lifecycle."""

    def test_returns_tracer_on_first_call(self):
        tracer = get_tracer()
        assert tracer is not None

    def test_returns_same_tracer_on_subsequent_calls(self):
        tracer1 = get_tracer()
        tracer2 = get_tracer()
        assert tracer1 is tracer2

    def test_returns_noop_when_otel_unavailable(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", False):
            reset_tracer()
            tracer = get_tracer()
            assert isinstance(tracer, _NoOpTracer)


class TestResetTracer:
    """Test reset_tracer clears singleton."""

    def test_reset_allows_reinitialization(self):
        tracer1 = get_tracer()
        reset_tracer()
        tracer2 = get_tracer()
        assert tracer1 is not tracer2 or isinstance(tracer2, _NoOpTracer)


class TestIsTracingAvailable:
    """Test is_tracing_available flag."""

    def test_returns_bool(self):
        result = is_tracing_available()
        assert isinstance(result, bool)


# ── Provider setup tests ────────────────────────────────────────────────────


class TestSetupProvider:
    """Test _setup_provider configuration via environment variables."""

    def test_exporter_none_returns_noop(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", True), \
             patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "none"}):
            reset_tracer()
            tracer = get_tracer()
            assert isinstance(tracer, _NoOpTracer)

    def test_unknown_exporter_returns_noop(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", True), \
             patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "invalid"}):
            reset_tracer()
            tracer = get_tracer()
            assert isinstance(tracer, _NoOpTracer)

    def test_console_exporter_initializes(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", True), \
             patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "console"}, clear=False):
            # Mock the OTel SDK imports to avoid requiring the package
            mock_provider = MagicMock()
            mock_tracer = MagicMock()
            mock_provider.get_tracer.return_value = mock_tracer

            with patch("src.monitoring.tracing.TracerProvider", return_value=mock_provider, create=True), \
                 patch("src.monitoring.tracing.BatchSpanProcessor", create=True), \
                 patch("src.monitoring.tracing.ConsoleSpanExporter", create=True), \
                 patch("src.monitoring.tracing.Resource", create=True) as mock_resource:
                mock_resource.create.return_value = MagicMock()
                reset_tracer()

                # Need to patch at module level where imports happen
                with patch.dict("sys.modules", {
                    "opentelemetry": MagicMock(),
                    "opentelemetry.sdk.trace": MagicMock(),
                    "opentelemetry.sdk.trace.export": MagicMock(),
                    "opentelemetry.sdk.resources": MagicMock(),
                }):
                    # If OTel is available but imports fail, we get noop
                    tracer = get_tracer()
                    assert tracer is not None

    def test_setup_exception_returns_noop(self):
        with patch.object(tracing_mod, "_OTEL_AVAILABLE", True), \
             patch.dict(os.environ, {"OTEL_TRACES_EXPORTER": "console"}):
            # Force an exception during provider setup
            with patch("src.monitoring.tracing.TracerProvider", side_effect=RuntimeError("setup fail"), create=True):
                reset_tracer()
                tracer = get_tracer()
                assert isinstance(tracer, _NoOpTracer)


# ── Health check status tests ───────────────────────────────────────────────


class TestGetTracingStatus:
    """Test get_tracing_status health-check helper."""

    def test_returns_dict_with_required_keys(self):
        status = get_tracing_status()
        assert "available" in status
        assert "initialized" in status
        assert "exporter" in status
        assert "service_name" in status

    def test_status_reflects_initialization_state(self):
        # Before get_tracer called
        status_before = get_tracing_status()
        assert status_before["initialized"] is False

        # After get_tracer called
        get_tracer()
        status_after = get_tracing_status()
        assert status_after["initialized"] is True

    def test_service_name_from_env(self):
        with patch.dict(os.environ, {"OTEL_SERVICE_NAME": "my-service"}):
            status = get_tracing_status()
            assert status["service_name"] == "my-service"

    def test_default_service_name(self):
        with patch.dict(os.environ, {}, clear=True):
            # Remove the key if it exists
            os.environ.pop("OTEL_SERVICE_NAME", None)
            status = get_tracing_status()
            assert status["service_name"] == "custombot"
