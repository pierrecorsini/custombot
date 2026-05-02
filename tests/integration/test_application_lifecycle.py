"""
test_application_lifecycle.py — Integration test for Application.run() full lifecycle.

Exercises the complete Application lifecycle end-to-end through the real
``run()`` method:

  1. _startup() — initializes shutdown manager, bot components, scheduler, channel
  2. channel.start() — begins message polling, delivers a test message
  3. _on_message() — handles the test message through preflight → handle → send
  4. shutdown signal — triggered after message delivery
  5. _shutdown_cleanup() — ordered cleanup of all components

Components that require external services (WhatsApp channel, LLM, scheduler)
are mocked. The GracefulShutdown manager is a real instance so that
shutdown signalling works through the actual event-wait mechanism.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import Application
from src.bot import BotConfig, PreflightResult
from src.builder import BotComponents
from src.channels.base import BaseChannel, IncomingMessage
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig, save_config
from src.config.config_watcher import ConfigChangeApplier, ConfigWatcher
from src.shutdown import GracefulShutdown


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_config(tmp_path: Path) -> Config:
    """Config pointing at tmp_path workspace with skills auto-load disabled."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
        ),
        skills_auto_load=False,
    )


def _make_msg(**overrides: Any) -> IncomingMessage:
    """Create an IncomingMessage with sensible defaults."""
    defaults = dict(
        message_id="msg-lifecycle-001",
        chat_id="chat-lifecycle@s.whatsapp.net",
        sender_id="sender-1@s.whatsapp.net",
        sender_name="Alice",
        text="Hello from integration test",
        timestamp=1700000000.0,
        channel_type="whatsapp",
        fromMe=False,
        toMe=True,
    )
    defaults.update(overrides)
    return IncomingMessage(**defaults)


def _make_mock_bot_components() -> MagicMock:
    """Create a mock BotComponents with all required fields."""
    components = MagicMock(spec=BotComponents)
    components.bot = AsyncMock()
    components.bot.validate_wiring = MagicMock()
    components.bot.recover_pending_messages = AsyncMock()
    components.bot.preflight_check = AsyncMock(
        return_value=PreflightResult(passed=True)
    )
    components.bot.handle_message = AsyncMock(return_value="Bot reply")
    components.bot.process_scheduled = AsyncMock()
    components.bot.stop_memory_monitoring = MagicMock()
    components.db = AsyncMock()
    components.vector_memory = MagicMock()
    components.vector_memory.close = MagicMock()
    components.project_store = MagicMock()
    components.project_store.close = MagicMock()
    components.token_usage = MagicMock()
    components.message_queue = AsyncMock()
    components.message_queue.close = AsyncMock()
    components.llm = AsyncMock()
    components.llm.close = AsyncMock()
    components.component_durations = {"Database": 0.01, "LLM Client": 0.02}
    return components


def _make_mock_channel() -> MagicMock:
    """Create a mock BaseChannel that behaves like WhatsAppChannel."""
    channel = MagicMock(spec=BaseChannel)
    channel.start = AsyncMock()
    channel.send_message = AsyncMock()
    channel.send_typing = AsyncMock()
    channel.close = AsyncMock()
    channel.request_shutdown = MagicMock()
    channel.wait_connected = AsyncMock()
    return channel


def _make_mock_scheduler() -> MagicMock:
    """Create a mock TaskScheduler."""
    scheduler = MagicMock()
    scheduler.configure = MagicMock()
    scheduler.set_on_send = MagicMock()
    scheduler.set_on_trigger = MagicMock()
    scheduler.load_all = MagicMock()
    scheduler.start = MagicMock()
    scheduler.stop = AsyncMock()
    return scheduler


# ─────────────────────────────────────────────────────────────────────────────
# Test: Full lifecycle via run()
# ─────────────────────────────────────────────────────────────────────────────


class TestApplicationRunLifecycle:
    """End-to-end integration test for Application.run() lifecycle."""

    @pytest.mark.asyncio
    async def test_full_lifecycle_startup_message_shutdown(self, tmp_path: Path) -> None:
        """
        Full lifecycle: startup → channel delivers message → message handled
        → shutdown triggered → cleanup executed.

        The mock channel.start() simulates delivering a message to the
        registered handler, then triggers shutdown via the GracefulShutdown
        event. Verifies every lifecycle phase executes correctly.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # Track lifecycle events
        lifecycle_events: list[str] = []

        # Record when the message handler is invoked
        original_handle_message = mock_components.bot.handle_message

        async def _tracked_handle_message(*args: Any, **kwargs: Any) -> str:
            lifecycle_events.append("handle_message_called")
            return await original_handle_message(*args, **kwargs)

        mock_components.bot.handle_message = _tracked_handle_message

        # Mock channel.start() to deliver a message then trigger shutdown
        async def _mock_channel_start(on_message_handler):
            lifecycle_events.append("channel_start_called")
            msg = _make_msg()
            await on_message_handler(msg)
            lifecycle_events.append("message_delivered")
            # Trigger shutdown after message delivery
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _mock_channel_start

        # Initialize _health_server since it's only set by _start_health_server()
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_shutdown,
        ):
            await app.run()

        # Verify startup completed — all components initialized
        assert app._shutdown_mgr is not None
        assert app._components is mock_components
        assert app._scheduler is mock_scheduler
        assert app._channel is mock_channel

        # Verify startup sequence: validate_wiring called after build
        mock_components.bot.validate_wiring.assert_called_once()

        # Verify scheduler was configured and started
        mock_scheduler.configure.assert_called_once()
        mock_scheduler.load_all.assert_called_once()
        mock_scheduler.start.assert_called_once()

        # Verify scheduler wiring callbacks were registered
        mock_scheduler.set_on_send.assert_called_once()
        mock_scheduler.set_on_trigger.assert_called_once()

        # Verify crash recovery was attempted
        mock_components.bot.recover_pending_messages.assert_awaited_once_with(
            channel=mock_channel
        )

        # Verify channel.start was called with the message handler
        assert "channel_start_called" in lifecycle_events

        # Verify message was handled through the pipeline
        assert "handle_message_called" in lifecycle_events
        mock_components.bot.preflight_check.assert_awaited_once()

        # Verify typing indicator was sent
        mock_channel.send_typing.assert_awaited_once()

        # Verify response was sent to the user
        mock_channel.send_message.assert_awaited_once_with(
            _make_msg().chat_id, "Bot reply"
        )

        # Verify message was delivered before shutdown
        assert "message_delivered" in lifecycle_events

        # Verify shutdown cleanup was called with all components
        mock_shutdown.assert_awaited_once()
        call_kwargs = mock_shutdown.call_args[1]
        assert call_kwargs["shutdown"] is app._shutdown_mgr
        assert call_kwargs["channel"] is mock_channel
        assert call_kwargs["scheduler"] is mock_scheduler
        assert call_kwargs["db"] is mock_components.db
        assert call_kwargs["bot"] is mock_components.bot

    @pytest.mark.asyncio
    async def test_shutdown_before_message_does_not_handle(self, tmp_path: Path) -> None:
        """
        If shutdown is requested before any message arrives, no messages
        are processed and cleanup still runs.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # Channel starts but immediately triggers shutdown (no messages)
        async def _immediate_shutdown(on_message_handler):
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _immediate_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await app.run()

        # No messages were handled
        mock_components.bot.preflight_check.assert_not_called()
        mock_components.bot.handle_message.assert_not_called()
        mock_channel.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_preflight_rejection_message_not_sent(self, tmp_path: Path) -> None:
        """
        When preflight rejects a message, the bot does not handle it
        and no response is sent, but the lifecycle continues.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(
            passed=False, reason="duplicate"
        )
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _deliver_rejected_then_shutdown(on_message_handler):
            msg = _make_msg()
            await on_message_handler(msg)
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _deliver_rejected_then_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await app.run()

        # Preflight was called but rejected
        mock_components.bot.preflight_check.assert_awaited_once()

        # handle_message was NOT called (preflight rejected)
        mock_components.bot.handle_message.assert_not_called()

        # No response sent
        mock_channel.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_handle_message_error_sends_error_response(self, tmp_path: Path) -> None:
        """
        When handle_message raises, an error message is sent to the user
        and the lifecycle continues through shutdown.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_components.bot.handle_message = AsyncMock(
            side_effect=RuntimeError("LLM timeout")
        )
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _deliver_error_then_shutdown(on_message_handler):
            msg = _make_msg()
            await on_message_handler(msg)
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _deliver_error_then_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch(
                "src.core.message_pipeline.format_user_error",
                return_value="Error occurred (corr-123)",
            ),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await app.run()

        # Error counter was incremented
        assert app._session_metrics.errors_count == 1

        # Error message was sent to the user
        mock_channel.send_message.assert_awaited_once()
        call_args = mock_channel.send_message.call_args
        assert call_args[0][0] == _make_msg().chat_id
        assert "Error occurred" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_multiple_messages_then_shutdown(self, tmp_path: Path) -> None:
        """
        Multiple messages are processed before shutdown is triggered.
        Each message goes through the full pipeline independently.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # Track how many messages were handled
        handled_ids: list[str] = []

        async def _tracked_handle(msg, **kwargs):
            handled_ids.append(msg.message_id)
            return f"Reply to {msg.message_id}"

        mock_components.bot.handle_message = _tracked_handle

        async def _deliver_three_then_shutdown(on_message_handler):
            for i in range(3):
                msg = _make_msg(
                    message_id=f"msg-batch-{i}",
                    text=f"Message {i}",
                )
                await on_message_handler(msg)
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _deliver_three_then_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await app.run()

        # All three messages were handled
        assert len(handled_ids) == 3
        assert handled_ids == ["msg-batch-0", "msg-batch-1", "msg-batch-2"]

        # Session metrics tracked all messages
        assert app._session_metrics.messages_processed == 3

        # send_message was called 3 times (one per message)
        assert mock_channel.send_message.await_count == 3


class TestApplicationRunSchedulerWiring:
    """Verify scheduler callbacks are wired correctly during the lifecycle."""

    @pytest.mark.asyncio
    async def test_on_send_callback_uses_channel_with_skip_delays(
        self, tmp_path: Path
    ) -> None:
        """
        The scheduler's on_send callback calls channel.send_message
        with skip_delays=True, verified through the full lifecycle.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _immediate_shutdown(on_message_handler):
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _immediate_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await app.run()

        # Extract the on_send callback and invoke it
        on_send = mock_scheduler.set_on_send.call_args[0][0]
        await on_send("chat-1", "Scheduled message")

        mock_channel.send_message.assert_awaited_once_with(
            "chat-1", "Scheduled message", skip_delays=True
        )

    @pytest.mark.asyncio
    async def test_on_trigger_callback_calls_process_scheduled(
        self, tmp_path: Path
    ) -> None:
        """
        The scheduler's on_trigger callback calls bot.process_scheduled
        with the channel passed in, verified through the full lifecycle.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _immediate_shutdown(on_message_handler):
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _immediate_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await app.run()

        # Extract the on_trigger callback and invoke it
        on_trigger = mock_scheduler.set_on_trigger.call_args[0][0]
        await on_trigger("chat-1", "Summarize today", None)

        mock_components.bot.process_scheduled.assert_awaited_once_with(
            "chat-1", "Summarize today", channel=mock_channel, prompt_hmac=None
        )


class TestApplicationRunHealthServer:
    """Verify health server integration during the lifecycle."""

    @pytest.mark.asyncio
    async def test_health_server_started_when_port_configured(
        self, tmp_path: Path
    ) -> None:
        """
        When health_port is provided, the health server is started during
        the lifecycle and passed to perform_shutdown.
        """
        config = _make_config(tmp_path)
        app = Application(config, health_port=9876)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_health_server = MagicMock()
        mock_health_server.start = AsyncMock()
        mock_health_server.stop = AsyncMock()

        async def _immediate_shutdown(on_message_handler):
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _immediate_shutdown

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.health.HealthServer", return_value=mock_health_server) as mock_cls,
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps,
        ):
            await app.run()

        # HealthServer was instantiated with components
        mock_cls.assert_called_once()
        hs_kwargs = mock_cls.call_args[1]
        assert hs_kwargs["db"] is mock_components.db
        assert hs_kwargs["scheduler"] is mock_scheduler

        # HealthServer.start was called with the configured port
        mock_health_server.start.assert_awaited_once_with(port=9876)

        # perform_shutdown received the health server
        ps_kwargs = mock_ps.call_args[1]
        assert ps_kwargs["health_server"] is mock_health_server

    @pytest.mark.asyncio
    async def test_no_health_server_when_port_not_configured(
        self, tmp_path: Path
    ) -> None:
        """
        When no health_port is provided, no health server is started.
        """
        config = _make_config(tmp_path)
        app = Application(config)  # no health_port

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _immediate_shutdown(on_message_handler):
            app.shutdown_mgr.request_shutdown()

        mock_channel.start = _immediate_shutdown
        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps,
        ):
            await app.run()

        # No health server passed to shutdown
        ps_kwargs = mock_ps.call_args[1]
        assert ps_kwargs.get("health_server") is None


class TestApplicationRunShutdownDuringQRWait:
    """Verify clean shutdown when Ctrl+C is pressed during QR-wait phase."""

    @pytest.mark.asyncio
    async def test_shutdown_during_qr_wait_cleans_up_without_leaks(
        self, tmp_path: Path
    ) -> None:
        """
        When shutdown is requested while the QR code is displayed (before
        WhatsApp connects), the channel exits cleanly and ``_shutdown_cleanup()``
        runs without resource leaks or unhandled exceptions.

        Simulates: user presses Ctrl+C before scanning the QR code.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # channel.start() hangs until shutdown is requested, then returns
        # (simulates QR-wait phase → user presses Ctrl+C → channel exits)
        async def _channel_start_wait_for_shutdown(on_message_handler):
            await app.shutdown_mgr.wait_for_shutdown()

        mock_channel.start = _channel_start_wait_for_shutdown

        # wait_connected() never resolves during QR-wait (no connection yet)
        async def _wait_connected_hang():
            await asyncio.Event().wait()  # never resolves

        mock_channel.wait_connected = _wait_connected_hang

        # Trigger shutdown after a small delay (simulates Ctrl+C)
        async def _trigger_shutdown():
            await asyncio.sleep(0.05)
            app.shutdown_mgr.request_shutdown()

        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_shutdown,
        ):
            # Run shutdown trigger alongside the app lifecycle
            await asyncio.gather(app.run(), _trigger_shutdown())

        # Shutdown cleanup was called — all components passed
        mock_shutdown.assert_awaited_once()
        call_kwargs = mock_shutdown.call_args[1]
        assert call_kwargs["shutdown"] is app._shutdown_mgr
        assert call_kwargs["channel"] is mock_channel
        assert call_kwargs["scheduler"] is mock_scheduler
        assert call_kwargs["db"] is mock_components.db

        # No messages were processed (connection never established)
        mock_components.bot.preflight_check.assert_not_called()
        mock_components.bot.handle_message.assert_not_called()
        mock_channel.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown_during_qr_wait_no_unhandled_exceptions(
        self, tmp_path: Path
    ) -> None:
        """
        Shutdown during QR-wait does not propagate exceptions — ``run()``
        returns cleanly even though the channel never connected.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _channel_start_wait_for_shutdown(on_message_handler):
            await app.shutdown_mgr.wait_for_shutdown()

        mock_channel.start = _channel_start_wait_for_shutdown
        mock_channel.wait_connected = AsyncMock(
            side_effect=asyncio.Event().wait
        )

        async def _trigger_shutdown():
            await asyncio.sleep(0.05)
            app.shutdown_mgr.request_shutdown()

        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            # Should complete without raising
            await asyncio.gather(app.run(), _trigger_shutdown())

    @pytest.mark.asyncio
    async def test_channel_start_exception_during_qr_wait_triggers_cleanup(
        self, tmp_path: Path
    ) -> None:
        """
        If ``channel.start()`` raises during QR-wait (e.g., neonize crash),
        ``_shutdown_cleanup()`` still runs and the exception is handled.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # channel.start() raises after a delay (simulates crash during QR-wait)
        async def _channel_start_crash(on_message_handler):
            await asyncio.sleep(0.05)
            raise ConnectionError("neonize process exited unexpectedly")

        mock_channel.start = _channel_start_crash
        mock_channel.wait_connected = AsyncMock(
            side_effect=asyncio.Event().wait
        )

        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_shutdown,
        ):
            await app.run()

        # Cleanup was called despite the channel crash
        mock_shutdown.assert_awaited_once()
        assert mock_shutdown.call_args[1]["channel"] is mock_channel

        # No messages were processed
        mock_components.bot.handle_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_startup_timeout_cancels_poll_task_and_cleans_up(
        self, tmp_path: Path
    ) -> None:
        """
        When the channel hangs during startup (neither connects nor exits
        within the timeout), ``run()`` cancels the poll task, cleans up,
        and returns without resource leaks.

        Exercises the timeout branch in ``run()`` (lines 142-152) where
        ``poll_task`` is NOT in ``done`` and gets cancelled — a code path
        with zero prior test coverage.

        Simulates: neonize bug or network issue that prevents QR display
        and blocks ``channel.start()`` indefinitely.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # channel.start() hangs indefinitely (simulates neonize bug)
        async def _channel_start_hang(on_message_handler):
            await asyncio.Event().wait()  # never resolves

        mock_channel.start = _channel_start_hang

        # wait_connected() also hangs (no connection established)
        async def _wait_connected_hang():
            await asyncio.Event().wait()

        mock_channel.wait_connected = _wait_connected_hang

        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_shutdown,
            patch("src.app.DEFAULT_CHANNEL_STARTUP_TIMEOUT", 0.1),
        ):
            # run() should return cleanly after the 0.1s timeout fires
            await app.run()

        # Shutdown cleanup was called with all components
        mock_shutdown.assert_awaited_once()
        call_kwargs = mock_shutdown.call_args[1]
        assert call_kwargs["shutdown"] is app._shutdown_mgr
        assert call_kwargs["channel"] is mock_channel
        assert call_kwargs["scheduler"] is mock_scheduler
        assert call_kwargs["db"] is mock_components.db

        # No messages were processed (connection never established)
        mock_components.bot.preflight_check.assert_not_called()
        mock_components.bot.handle_message.assert_not_called()
        mock_channel.send_message.assert_not_called()

        # The timeout path does NOT call request_shutdown() — it aborts
        # startup directly via _shutdown_cleanup().  So accepting_messages
        # remains True (no graceful shutdown was initiated, just a startup
        # abort).  This is correct: the app exits but was never "listening".
        assert app.shutdown_mgr.accepting_messages is True

    @pytest.mark.asyncio
    async def test_shutdown_during_qr_wait_rejects_incoming_messages(
        self, tmp_path: Path
    ) -> None:
        """
        After shutdown is requested during QR-wait, the ``_on_message()``
        handler rejects any queued messages because ``accepting_messages``
        is False.  Verifies the gate works during the shutdown window.
        """
        config = _make_config(tmp_path)
        app = Application(config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # Track whether _on_message was called during shutdown
        messages_seen: list[str] = []

        # channel.start() waits for shutdown, then returns
        async def _channel_start_wait_for_shutdown(on_message_handler):
            await app.shutdown_mgr.wait_for_shutdown()
            # After shutdown is set, try to deliver a message
            # This simulates a message arriving in the queue just
            # before the channel fully stops
            msg = _make_msg(message_id="late-msg")
            await on_message_handler(msg)
            messages_seen.append("late_msg_delivered")

        mock_channel.start = _channel_start_wait_for_shutdown

        # wait_connected() never resolves during QR-wait
        async def _wait_connected_hang():
            await asyncio.Event().wait()

        mock_channel.wait_connected = _wait_connected_hang

        async def _trigger_shutdown():
            await asyncio.sleep(0.05)
            app.shutdown_mgr.request_shutdown()

        app._health_server = None

        with (
            patch("src.builder.build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
            patch("src.app.cli_output"),
            patch("src.app.perform_shutdown", new_callable=AsyncMock),
        ):
            await asyncio.gather(app.run(), _trigger_shutdown())

        # The late message was delivered to _on_message but rejected
        # because accepting_messages was False
        assert "late_msg_delivered" in messages_seen

        # Neither preflight nor handle_message were called — message
        # was rejected at the accepting_messages gate in _on_message
        mock_components.bot.preflight_check.assert_not_called()
        mock_components.bot.handle_message.assert_not_called()
        mock_channel.send_message.assert_not_called()


class TestConfigHotReload:
    """Integration test for ConfigWatcher hot-reload applying changes to live components.

    Exercises the full hot-reload pipeline: ConfigWatcher detects a config
    file change via mtime polling → loads and validates the new config →
    ConfigChangeApplier diffs old vs new → safe fields are applied to
    live components (Bot, Channel, LLM) without restart.
    """

    @pytest.mark.asyncio
    async def test_max_tool_iterations_change_applied_without_restart(
        self, tmp_path: Path
    ) -> None:
        """
        Changing ``max_tool_iterations`` in config.json is detected by the
        watcher and applied to the bot's ``BotConfig`` without restart.

        Verifies:
            a) ConfigWatcher detects the file change via mtime polling
            b) ConfigChangeApplier rebuilds BotConfig with the new value
            c) The bot instance reflects the new config immediately
        """
        # ── Arrange ──
        config_path = tmp_path / "config.json"

        initial_config = Config(
            llm=LLMConfig(
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-hot-reload",
                max_tool_iterations=10,
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )
        save_config(initial_config, config_path)

        # Mock bot with a real BotConfig that the applier will replace
        mock_bot = MagicMock()
        mock_bot._cfg = BotConfig(
            max_tool_iterations=10,
            memory_max_history=100,
            system_prompt_prefix="",
            stream_response=False,
        )

        mock_channel = MagicMock()
        mock_channel.apply_channel_config = MagicMock()

        mock_llm = MagicMock()
        mock_llm._cfg = initial_config.llm

        shutdown_mgr = GracefulShutdown(timeout=30.0)
        reconfigure_logging = MagicMock()

        # Reloaded config is stored here for the watcher's internal state
        reloaded_config = initial_config

        applier = ConfigChangeApplier(
            app_config=reloaded_config,
            bot=mock_bot,
            channel=mock_channel,
            llm=mock_llm,
            shutdown_mgr=shutdown_mgr,
            reconfigure_logging=reconfigure_logging,
        )

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial_config,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        watcher.start()
        try:
            # Let the watcher settle (one poll cycle)
            await asyncio.sleep(0.1)

            # Sanity: bot still has the original value
            assert mock_bot._cfg.max_tool_iterations == 10

            # ── Act: write updated config to disk ──
            updated_config = Config(
                llm=LLMConfig(
                    model="gpt-4o",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test-hot-reload",
                    max_tool_iterations=5,
                ),
                whatsapp=WhatsAppConfig(
                    provider="neonize",
                    neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
                ),
                skills_auto_load=False,
            )
            save_config(updated_config, config_path)

            # Wait for the watcher to detect, load, validate, and apply
            await asyncio.sleep(0.5)

            # ── Assert ──
            assert mock_bot._cfg.max_tool_iterations == 5, (
                f"Expected max_tool_iterations=5 after hot-reload, "
                f"got {mock_bot._cfg.max_tool_iterations}"
            )
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_temperature_change_applied_to_llm_without_restart(
        self, tmp_path: Path
    ) -> None:
        """
        Changing ``llm.temperature`` in config.json is applied to the
        LLM client's config reference without restart.
        """
        # ── Arrange ──
        config_path = tmp_path / "config.json"

        initial_config = Config(
            llm=LLMConfig(
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-hot-reload",
                temperature=0.7,
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )
        save_config(initial_config, config_path)

        mock_bot = MagicMock()
        mock_bot._cfg = BotConfig(
            max_tool_iterations=10,
            memory_max_history=100,
            system_prompt_prefix="",
            stream_response=False,
        )

        mock_channel = MagicMock()
        mock_channel.apply_channel_config = MagicMock()

        mock_llm = MagicMock()
        mock_llm._cfg = initial_config.llm

        shutdown_mgr = GracefulShutdown(timeout=30.0)
        reconfigure_logging = MagicMock()

        applier = ConfigChangeApplier(
            app_config=initial_config,
            bot=mock_bot,
            channel=mock_channel,
            llm=mock_llm,
            shutdown_mgr=shutdown_mgr,
            reconfigure_logging=reconfigure_logging,
        )

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial_config,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            # ── Act: change temperature ──
            updated_config = Config(
                llm=LLMConfig(
                    model="gpt-4o",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test-hot-reload",
                    temperature=0.3,
                ),
                whatsapp=WhatsAppConfig(
                    provider="neonize",
                    neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
                ),
                skills_auto_load=False,
            )
            save_config(updated_config, config_path)

            await asyncio.sleep(0.5)

            # ── Assert: LLM config was updated ──
            assert mock_llm._cfg.temperature == 0.3, (
                f"Expected temperature=0.3 after hot-reload, "
                f"got {mock_llm._cfg.temperature}"
            )
        finally:
            await watcher.stop()

    @pytest.mark.asyncio
    async def test_destructive_field_change_not_applied(
        self, tmp_path: Path
    ) -> None:
        """
        Changing a destructive field (e.g., ``llm.model``) is NOT applied
        to live components — only a warning is logged.
        """
        # ── Arrange ──
        config_path = tmp_path / "config.json"

        initial_config = Config(
            llm=LLMConfig(
                model="gpt-4o",
                base_url="https://api.openai.com/v1",
                api_key="sk-test-hot-reload",
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
            ),
            skills_auto_load=False,
        )
        save_config(initial_config, config_path)

        mock_bot = MagicMock()
        mock_bot._cfg = BotConfig(
            max_tool_iterations=10,
            memory_max_history=100,
            system_prompt_prefix="",
            stream_response=False,
        )

        mock_channel = MagicMock()
        mock_channel.apply_channel_config = MagicMock()

        mock_llm = MagicMock()
        mock_llm._cfg = initial_config.llm

        shutdown_mgr = GracefulShutdown(timeout=30.0)
        reconfigure_logging = MagicMock()

        applier = ConfigChangeApplier(
            app_config=initial_config,
            bot=mock_bot,
            channel=mock_channel,
            llm=mock_llm,
            shutdown_mgr=shutdown_mgr,
            reconfigure_logging=reconfigure_logging,
        )

        watcher = ConfigWatcher(
            config_path=config_path,
            current_config=initial_config,
            applier=applier,
            poll_interval=0.05,
            debounce=0.0,
        )

        watcher.start()
        try:
            await asyncio.sleep(0.1)

            original_model = mock_llm._cfg.model

            # ── Act: change destructive field (llm.model) ──
            updated_config = Config(
                llm=LLMConfig(
                    model="gpt-3.5-turbo",
                    base_url="https://api.openai.com/v1",
                    api_key="sk-test-hot-reload",
                ),
                whatsapp=WhatsAppConfig(
                    provider="neonize",
                    neonize=NeonizeConfig(db_path=str(tmp_path / "session.db")),
                ),
                skills_auto_load=False,
            )
            save_config(updated_config, config_path)

            await asyncio.sleep(0.5)

            # ── Assert: model was NOT changed on the LLM client ──
            # The applier only warns about destructive changes
            assert mock_llm._cfg.model == original_model
        finally:
            await watcher.stop()
