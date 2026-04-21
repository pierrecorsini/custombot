"""
Tests for src/app.py — Application class lifecycle.

Covers:
- _startup() initializes all components in the correct order
- _wire_scheduler() connects scheduler callbacks to the bot and channel
- _on_message() handles preflight → typing → handle → send flow
- _on_message() rejects during shutdown and handles errors
- _shutdown_cleanup() delegates to perform_shutdown with all components
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import Application
from src.bot import PreflightResult
from src.channels.base import BaseChannel, IncomingMessage
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig
from src.core.message_pipeline import (
    ErrorHandlerMiddleware,
    HandleMessageMiddleware,
    InboundLoggingMiddleware,
    MessagePipeline,
    MetricsMiddleware,
    OperationTrackerMiddleware,
    PreflightMiddleware,
    TypingMiddleware,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def test_config(tmp_path: Path) -> Config:
    """Config with workspace in tmp_path and skills_auto_load disabled."""
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
        message_id="msg-1",
        chat_id="chat-1@s.whatsapp.net",
        sender_id="sender-1@s.whatsapp.net",
        sender_name="Alice",
        text="Hello",
        timestamp=1700000000.0,
        channel_type="whatsapp",
        fromMe=False,
        toMe=True,
    )
    defaults.update(overrides)
    return IncomingMessage(**defaults)


def _make_mock_bot_components() -> MagicMock:
    """Create a mock BotComponents with all fields."""
    components = MagicMock()
    components.bot = AsyncMock()
    components.bot.validate_wiring = MagicMock()
    components.bot.recover_pending_messages = AsyncMock()
    components.bot.preflight_check = AsyncMock()
    components.bot.handle_message = AsyncMock()
    components.db = AsyncMock()
    components.vector_memory = MagicMock()
    components.project_store = MagicMock()
    components.token_usage = MagicMock()
    components.message_queue = AsyncMock()
    components.llm = AsyncMock()
    components.component_durations = {}
    return components


def _make_mock_channel() -> MagicMock:
    """Create a mock BaseChannel."""
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


def _build_test_pipeline(
    app: Application,
    mock_components: MagicMock,
    mock_channel: MagicMock,
    mock_shutdown: MagicMock,
) -> MessagePipeline:
    """Build a pipeline wired to the given mocks."""
    return MessagePipeline([
        OperationTrackerMiddleware(mock_shutdown),
        MetricsMiddleware(app._session_metrics),
        InboundLoggingMiddleware(),
        PreflightMiddleware(mock_components.bot),
        TypingMiddleware(mock_channel),
        ErrorHandlerMiddleware(
            mock_channel, app._session_metrics, verbose=False
        ),
        HandleMessageMiddleware(mock_components.bot, mock_channel),
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _startup() phase
# ─────────────────────────────────────────────────────────────────────────────


class TestStartup:
    """Tests for Application._startup() component initialization order."""

    async def test_creates_shutdown_manager(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        assert app._shutdown_mgr is not None
        assert app.shutdown_mgr.accepting_messages is True

    async def test_builds_bot_components(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components) as mock_build,
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        mock_build.assert_awaited_once_with(
            test_config, session_metrics=app._session_metrics
        )
        assert app._components is mock_components

    async def test_validates_wiring_after_build(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        mock_components.bot.validate_wiring.assert_called_once()

    async def test_creates_scheduler(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        assert app._scheduler is mock_scheduler
        mock_scheduler.configure.assert_called_once()
        mock_scheduler.load_all.assert_called_once()
        mock_scheduler.start.assert_called_once()

    async def test_creates_whatsapp_channel(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel) as mock_cls,
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        mock_cls.assert_called_once_with(
            test_config.whatsapp,
            safe_mode=False,
            load_history=test_config.load_history,
        )
        assert app._channel is mock_channel

    async def test_wires_scheduler(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        mock_scheduler.set_on_send.assert_called_once()
        mock_scheduler.set_on_trigger.assert_called_once()

    async def test_recovers_pending_messages(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel),
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        mock_components.bot.recover_pending_messages.assert_awaited_once_with(
            channel=mock_channel
        )

    async def test_safe_mode_forwarded_to_channel(self, test_config: Config) -> None:
        app = Application(test_config, safe_mode=True)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        with (
            patch("src.app._build_bot", return_value=mock_components),
            patch("src.channels.whatsapp.WhatsAppChannel", return_value=mock_channel) as mock_cls,
            patch("src.app.TaskScheduler", return_value=mock_scheduler),
            patch("src.app.set_scheduler_instance"),
            patch("src.app._log_startup_begin", return_value=0.0),
            patch("src.app._log_component_init"),
            patch("src.app._log_component_ready"),
            patch("src.app._log_startup_complete"),
        ):
            await app._startup()

        _, kwargs = mock_cls.call_args
        assert kwargs["safe_mode"] is True


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _wire_scheduler()
# ─────────────────────────────────────────────────────────────────────────────


class TestWireScheduler:
    """Tests for Application._wire_scheduler() callback wiring."""

    async def test_set_on_send_calls_channel_send_with_skip_delays(
        self, test_config: Config
    ) -> None:
        app = Application(test_config)
        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        # Simulate post-startup state
        app._channel = mock_channel
        app._components = mock_components
        app._scheduler = mock_scheduler

        app._wire_scheduler()

        on_send = mock_scheduler.set_on_send.call_args[0][0]
        await on_send("chat-1", "hello")

        mock_channel.send_message.assert_awaited_once_with(
            "chat-1", "hello", skip_delays=True
        )

    async def test_set_on_trigger_calls_bot_process_scheduled(
        self, test_config: Config
    ) -> None:
        app = Application(test_config)
        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        app._channel = mock_channel
        app._components = mock_components
        app._scheduler = mock_scheduler

        app._wire_scheduler()

        on_trigger = mock_scheduler.set_on_trigger.call_args[0][0]
        await on_trigger("chat-1", "Summarize today's events")

        mock_components.bot.process_scheduled.assert_awaited_once_with(
            "chat-1", "Summarize today's events", channel=mock_channel
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _on_message() — happy path
# ─────────────────────────────────────────────────────────────────────────────


class TestOnMessageHappyPath:
    """Tests for Application._on_message() happy-path flow."""

    async def test_increments_message_counter(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.return_value = "Hi there!"

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        assert app._session_metrics.messages_processed == 1

    async def test_calls_preflight_check(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.return_value = "Reply"

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        mock_components.bot.preflight_check.assert_awaited_once_with(msg)

    async def test_sends_typing_indicator(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.return_value = "Reply"

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        mock_channel.send_typing.assert_awaited_once_with(msg.chat_id)

    async def test_handles_message_and_sends_response(
        self, test_config: Config
    ) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.return_value = "Bot response"

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        mock_components.bot.handle_message.assert_awaited_once()
        mock_channel.send_message.assert_awaited_once_with(
            msg.chat_id, "Bot response"
        )

    async def test_does_not_send_empty_response(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.return_value = None

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        # Only typing was sent, no response message
        mock_channel.send_message.assert_not_called()

    async def test_exits_operation_in_finally(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.return_value = "Reply"

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=42)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        mock_shutdown.exit_operation.assert_awaited_once_with(42)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _on_message() — rejection paths
# ─────────────────────────────────────────────────────────────────────────────


class TestOnMessageRejection:
    """Tests for _on_message() when messages are rejected."""

    async def test_rejects_during_shutdown(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = False

        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        # No message counter increment
        assert app._session_metrics.messages_processed == 0

    async def test_rejects_when_enter_operation_returns_none(
        self, test_config: Config
    ) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=None)

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        mock_components.bot.preflight_check.assert_not_called()

    async def test_rejects_when_preflight_fails(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(
            passed=False, reason="dedup"
        )

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        mock_components.bot.handle_message.assert_not_called()
        mock_channel.send_message.assert_not_called()
        mock_shutdown.exit_operation.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _on_message() — error handling
# ─────────────────────────────────────────────────────────────────────────────


class TestOnMessageErrors:
    """Tests for _on_message() error handling."""

    async def test_increments_error_counter_on_exception(
        self, test_config: Config
    ) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.side_effect = RuntimeError("LLM failed")

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        await app._on_message(msg)

        assert app._session_metrics.errors_count == 1

    async def test_sends_error_message_to_user(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.side_effect = RuntimeError("boom")

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        with patch("src.app.format_user_error", return_value="Error occurred"):
            await app._on_message(msg)

        # Error message sent to user
        assert mock_channel.send_message.await_count == 1
        call_args = mock_channel.send_message.call_args
        assert call_args[0][0] == msg.chat_id

    async def test_handles_channel_disconnect_during_error_send(
        self, test_config: Config
    ) -> None:
        """If the channel is disconnected when sending error, no re-raise."""
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.side_effect = RuntimeError("LLM down")

        mock_channel = _make_mock_channel()
        # First call is the error message send, which also fails
        mock_channel.send_message.side_effect = ConnectionError("channel gone")

        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        with patch("src.app.format_user_error", return_value="Error"):
            # Should NOT raise — secondary failure is caught
            await app._on_message(msg)

    async def test_exits_operation_on_error_path(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_components = _make_mock_bot_components()
        mock_components.bot.preflight_check.return_value = PreflightResult(passed=True)
        mock_components.bot.handle_message.side_effect = RuntimeError("fail")

        mock_channel = _make_mock_channel()
        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True
        mock_shutdown.enter_operation = AsyncMock(return_value=1)
        mock_shutdown.exit_operation = AsyncMock()

        app._components = mock_components
        app._channel = mock_channel
        app._shutdown_mgr = mock_shutdown

        with patch("src.app.format_user_error", return_value="Error"):
            await app._on_message(msg)

        mock_shutdown.exit_operation.assert_awaited_once_with(1)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _shutdown_cleanup()
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownCleanup:
    """Tests for Application._shutdown_cleanup() delegating to perform_shutdown."""

    async def test_calls_perform_shutdown_with_all_components(
        self, test_config: Config
    ) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()

        app._components = mock_components
        app._channel = mock_channel
        app._scheduler = mock_scheduler
        app._shutdown_mgr = mock_shutdown
        app._health_server = None

        with patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps:
            await app._shutdown_cleanup()

        mock_ps.assert_awaited_once()
        call_kwargs = mock_ps.call_args[1]
        assert call_kwargs["shutdown"] is mock_shutdown
        assert call_kwargs["channel"] is mock_channel
        assert call_kwargs["scheduler"] is mock_scheduler
        assert call_kwargs["db"] is mock_components.db
        assert call_kwargs["vector_memory"] is mock_components.vector_memory
        assert call_kwargs["project_store"] is mock_components.project_store
        assert call_kwargs["message_queue"] is mock_components.message_queue
        assert call_kwargs["llm"] is mock_components.llm
        assert "session_metrics" in call_kwargs

    async def test_passes_health_server(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()
        mock_health = MagicMock()

        app._components = mock_components
        app._channel = mock_channel
        app._scheduler = mock_scheduler
        app._shutdown_mgr = mock_shutdown
        app._health_server = mock_health

        with patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps:
            await app._shutdown_cleanup()

        call_kwargs = mock_ps.call_args[1]
        assert call_kwargs["health_server"] is mock_health


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Property accessors (pre-startup guards)
# ─────────────────────────────────────────────────────────────────────────────


class TestPropertyGuards:
    """Tests that property accessors raise before _startup() is called."""

    def test_channel_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Channel not initialized"):
            _ = app.channel

    def test_components_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Components not initialized"):
            _ = app.components

    def test_shutdown_mgr_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Shutdown manager not initialized"):
            _ = app.shutdown_mgr

    def test_scheduler_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Scheduler not initialized"):
            _ = app.scheduler
