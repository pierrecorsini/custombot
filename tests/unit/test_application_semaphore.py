"""
Tests for Application._on_message semaphore concurrency control.

Covers:
- Single message acquires and releases the semaphore cleanly
- Concurrent messages respect the max_concurrent_messages limit
- Messages rejected before acquiring the semaphore during shutdown
- Messages rejected after acquiring the semaphore during shutdown
- Pipeline exceptions propagate after semaphore is released
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import Application, AppPhase


class TestOnMessageSemaphoreConcurrency:
    """Verify _on_message respects the concurrency semaphore."""

    @pytest.fixture
    def mock_app(self) -> Application:
        """Create an Application with mocked internals and max_concurrent_messages=2."""
        config = MagicMock()
        config.max_concurrent_messages = 2
        app = Application.__new__(Application)
        app._config = config
        app._verbose = False
        app._phase = AppPhase.RUNNING

        # Create a real semaphore
        app._message_semaphore = asyncio.Semaphore(2)

        # Mock state with shutdown_mgr and pipeline
        mock_state = MagicMock()
        mock_state.shutdown_mgr = MagicMock()
        mock_state.shutdown_mgr.accepting_messages = True
        mock_pipeline = AsyncMock()
        mock_state.pipeline = mock_pipeline
        app._state = mock_state

        return app

    @pytest.mark.asyncio
    async def test_single_message_acquires_semaphore(self, mock_app: Application):
        """A single message acquires and releases the semaphore."""
        msg = MagicMock()
        msg.chat_id = "chat_1"
        msg.correlation_id = "corr_1"

        await Application._on_message(mock_app, msg)

        # Pipeline was called
        mock_app._state.pipeline.execute.assert_awaited_once()
        # Semaphore fully released
        assert mock_app._message_semaphore._value == 2

    @pytest.mark.asyncio
    async def test_concurrent_messages_respect_semaphore_limit(
        self, mock_app: Application
    ):
        """When max_concurrent_messages=2, a third message waits until a slot frees."""
        block_event = asyncio.Event()
        call_order: list[str] = []

        async def slow_execute(ctx):
            call_order.append(f"start_{ctx.msg.chat_id}")
            await block_event.wait()
            call_order.append(f"end_{ctx.msg.chat_id}")

        mock_app._state.pipeline.execute = slow_execute

        msgs = []
        for i in range(3):
            msg = MagicMock()
            msg.chat_id = f"chat_{i}"
            msg.correlation_id = f"corr_{i}"
            msgs.append(msg)

        # Start all three messages concurrently
        tasks = [
            asyncio.create_task(Application._on_message(mock_app, m)) for m in msgs
        ]

        # Allow event loop to schedule — first two should start, third should wait
        await asyncio.sleep(0.05)

        # Only 2 should have started (semaphore limit)
        started = [s for s in call_order if s.startswith("start_")]
        assert len(started) == 2, f"Expected 2 concurrent starts, got {len(started)}"

        # Unblock all
        block_event.set()
        await asyncio.gather(*tasks)

        # All three should have completed
        ended = [s for s in call_order if s.startswith("end_")]
        assert len(ended) == 3
        assert mock_app._message_semaphore._value == 2

    @pytest.mark.asyncio
    async def test_rejected_during_shutdown(self, mock_app: Application):
        """Messages are rejected without acquiring the semaphore when shutdown is in progress."""
        mock_app._state.shutdown_mgr.accepting_messages = False

        msg = MagicMock()
        msg.chat_id = "chat_1"
        msg.correlation_id = "corr_1"

        await Application._on_message(mock_app, msg)

        # Pipeline should NOT have been called
        mock_app._state.pipeline.execute.assert_not_awaited()
        assert mock_app._message_semaphore._value == 2

    @pytest.mark.asyncio
    async def test_rejected_after_acquiring_semaphore_during_shutdown(
        self, mock_app: Application
    ):
        """Messages that queued before shutdown but acquired the semaphore after
        are rejected without calling the pipeline."""
        mock_app._state.shutdown_mgr.accepting_messages = True
        # Use a semaphore of size 1 for deterministic ordering
        mock_app._message_semaphore = asyncio.Semaphore(1)

        block = asyncio.Event()
        call_count = 0

        async def _slow_execute(ctx):
            nonlocal call_count
            call_count += 1
            await block.wait()

        mock_app._state.pipeline.execute = _slow_execute

        # First message occupies the single semaphore slot
        first = asyncio.create_task(Application._on_message(mock_app, MagicMock(chat_id="c1", correlation_id="r1")))
        await asyncio.sleep(0.05)

        # Second message queues at the semaphore
        second = asyncio.create_task(Application._on_message(mock_app, MagicMock(chat_id="c2", correlation_id="r2")))

        # Trigger shutdown while second is queued
        mock_app._state.shutdown_mgr.accepting_messages = False
        await asyncio.sleep(0.05)

        # Unblock first — second will acquire semaphore but see shutdown
        block.set()
        await asyncio.sleep(0.05)

        # Only the first message ran; second was rejected after acquiring semaphore
        assert call_count == 1

        # Clean up tasks
        first.cancel()
        second.cancel()
        try:
            await asyncio.gather(first, second, return_exceptions=True)
        except Exception:
            pass

        # Semaphore fully released
        assert mock_app._message_semaphore._value == 1

    @pytest.mark.asyncio
    async def test_pipeline_exception_propagates(self, mock_app: Application):
        """Exceptions from the pipeline are propagated after semaphore release."""
        mock_app._state.pipeline.execute = AsyncMock(
            side_effect=RuntimeError("pipeline failed")
        )

        msg = MagicMock()
        msg.chat_id = "chat_1"
        msg.correlation_id = "corr_1"

        with (
            patch("src.app.emit_error_event", new_callable=AsyncMock),
        ):
            with pytest.raises(RuntimeError, match="pipeline failed"):
                await Application._on_message(mock_app, msg)

        # Semaphore should be fully released despite the exception
        assert mock_app._message_semaphore._value == 2
