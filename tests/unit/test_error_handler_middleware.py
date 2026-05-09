"""
test_error_handler_middleware.py — Error-reply rate limiting tests for ErrorHandlerMiddleware.

Verifies that:
- Error replies are sent up to ``_ERROR_REPLY_MAX_LIMIT`` per chat
- After the threshold, replies are suppressed (not sent via send_and_track)
- Errors are still counted in metrics even when replies are suppressed
- Different chat_ids have independent rate-limit trackers
- Suppressed replies produce a warning log
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.message_pipeline import (
    ErrorHandlerMiddleware,
    MessageContext,
    _ERROR_REPLY_MAX_LIMIT,
    _ERROR_REPLY_WINDOW_SECONDS,
)

from tests.unit.conftest import make_message


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_ctx(chat_id: str = "chat_123") -> MessageContext:
    """Build a ``MessageContext`` with a specific chat_id."""
    return MessageContext(msg=make_message(chat_id=chat_id))


def _raise_error() -> None:
    """Callable that raises, simulating a downstream middleware failure."""
    raise RuntimeError("boom")


async def _raising_call_next() -> None:
    """Async callable that raises, simulating a downstream middleware failure."""
    raise RuntimeError("boom")


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestErrorHandlerMiddlewareRateLimiting:
    """Verify per-chat error-reply rate limiting."""

    @pytest.mark.asyncio
    async def test_sends_error_reply_below_threshold(self) -> None:
        """Error replies are sent when under the rate limit."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)
        ctx = _make_ctx()

        await middleware(ctx, _raising_call_next)

        channel.send_and_track.assert_called_once()
        assert metrics.increment_errors.call_count == 1

    @pytest.mark.asyncio
    async def test_sends_up_to_max_limit_error_replies(self) -> None:
        """Exactly _ERROR_REPLY_MAX_LIMIT error replies are sent before suppression."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        for i in range(_ERROR_REPLY_MAX_LIMIT):
            await middleware(_make_ctx(), _raising_call_next)

        assert channel.send_and_track.call_count == _ERROR_REPLY_MAX_LIMIT
        assert metrics.increment_errors.call_count == _ERROR_REPLY_MAX_LIMIT

    @pytest.mark.asyncio
    async def test_suppresses_error_reply_after_threshold(self) -> None:
        """After _ERROR_REPLY_MAX_LIMIT errors, replies are suppressed."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        # Exhaust the rate limit
        for _ in range(_ERROR_REPLY_MAX_LIMIT):
            await middleware(_make_ctx(), _raising_call_next)

        # Clear to isolate the suppressed call
        channel.send_and_track.reset_mock()

        # One more error from the same chat — should be suppressed
        await middleware(_make_ctx(), _raising_call_next)

        channel.send_and_track.assert_not_called()

    @pytest.mark.asyncio
    async def test_metrics_incremented_even_when_suppressed(self) -> None:
        """Errors are counted in metrics regardless of rate-limit suppression."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        total_errors = _ERROR_REPLY_MAX_LIMIT + 3

        for _ in range(total_errors):
            await middleware(_make_ctx(), _raising_call_next)

        # All errors must be counted, even those that were rate-limited
        assert metrics.increment_errors.call_count == total_errors

    @pytest.mark.asyncio
    async def test_suppressed_replies_produce_warning_log(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Suppressed error replies emit a WARNING log."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        # Exhaust the rate limit
        for _ in range(_ERROR_REPLY_MAX_LIMIT):
            await middleware(_make_ctx(), _raising_call_next)

        # Next error should log a warning
        with caplog.at_level(logging.WARNING, logger="src.core.message_pipeline"):
            await middleware(_make_ctx(), _raising_call_next)

        assert "rate limit exceeded" in caplog.text.lower()
        assert "suppressing" in caplog.text.lower()

    @pytest.mark.asyncio
    async def test_different_chats_tracked_independently(self) -> None:
        """Rate limiting is per-chat: one chat hitting the limit doesn't affect another."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        # Exhaust rate limit for chat_A
        for _ in range(_ERROR_REPLY_MAX_LIMIT):
            await middleware(_make_ctx(chat_id="chat_A"), _raising_call_next)

        # chat_B should still be allowed
        channel.send_and_track.reset_mock()
        await middleware(_make_ctx(chat_id="chat_B"), _raising_call_next)

        channel.send_and_track.assert_called_once()

    @pytest.mark.asyncio
    async def test_rate_limit_resets_after_window_expiry(self) -> None:
        """Error replies resume after the sliding window expires."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        with patch("time.monotonic", return_value=100.0):
            for _ in range(_ERROR_REPLY_MAX_LIMIT):
                await middleware(_make_ctx(), _raising_call_next)

        # Exhausted — suppress
        channel.send_and_track.reset_mock()
        with patch("time.monotonic", return_value=100.0):
            await middleware(_make_ctx(), _raising_call_next)

        channel.send_and_track.assert_not_called()

        # After window expires, replies should resume
        channel.send_and_track.reset_mock()
        with patch("time.monotonic", return_value=100.0 + _ERROR_REPLY_WINDOW_SECONDS + 1):
            await middleware(_make_ctx(), _raising_call_next)

        channel.send_and_track.assert_called_once()

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised_without_reply(self) -> None:
        """CancelledError is re-raised without sending an error reply."""
        channel = AsyncMock()
        channel.send_and_track = AsyncMock()
        metrics = MagicMock()

        middleware = ErrorHandlerMiddleware(channel=channel, metrics=metrics)

        async def _cancel_call_next() -> None:
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await middleware(_make_ctx(), _cancel_call_next)

        channel.send_and_track.assert_not_called()
        metrics.increment_errors.assert_not_called()
