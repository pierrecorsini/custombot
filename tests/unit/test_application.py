"""
Tests for src/app.py — Application class lifecycle.

Covers:
- _startup() delegates to StartupOrchestrator.run_all
- _wire_scheduler() connects scheduler callbacks to the bot and channel
- _on_message() delegates to self.state.pipeline.execute(ctx)
- _shutdown_cleanup() delegates to perform_shutdown with all components
- Property guards raise before startup completes
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.app import AppComponents, AppPhase, Application
from src.channels.base import BaseChannel, IncomingMessage
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig
from src.core.startup import StartupOrchestrator

if TYPE_CHECKING:
    from pathlib import Path


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
    components.bot.process_scheduled = AsyncMock()
    components.db = AsyncMock()
    components.vector_memory = MagicMock()
    components.project_store = MagicMock()
    components.token_usage = MagicMock()
    components.message_queue = AsyncMock()
    components.llm = AsyncMock()
    components.dedup = MagicMock()
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
    scheduler.set_dedup_service = MagicMock()
    return scheduler


def _make_mock_app_components(**overrides: Any) -> AppComponents:
    """Create an AppComponents instance with all required mock fields.

    Accepts optional overrides for any field.
    """
    defaults = dict(
        shutdown_mgr=MagicMock(),
        components=_make_mock_bot_components(),
        scheduler=_make_mock_scheduler(),
        channel=_make_mock_channel(),
        pipeline=MagicMock(),
        executor=MagicMock(spec=ThreadPoolExecutor),
        workspace_monitor=MagicMock(),
        config_watcher=MagicMock(),
    )
    defaults.update(overrides)
    return AppComponents(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _startup() phase
# ─────────────────────────────────────────────────────────────────────────────


class TestStartup:
    """Tests for Application._startup() via StartupOrchestrator delegation."""

    async def test_transitions_created_to_starting(self, test_config: Config) -> None:
        app = Application(test_config)
        assert app._phase == AppPhase.CREATED

        async def _fake_run_all(self_orch):
            ctx = self_orch._ctx
            ctx.shutdown_mgr = MagicMock()
            ctx.components = _make_mock_bot_components()
            ctx.scheduler = _make_mock_scheduler()
            ctx.channel = _make_mock_channel()
            ctx.pipeline = MagicMock()
            ctx.executor = MagicMock(spec=ThreadPoolExecutor)
            ctx.workspace_monitor = MagicMock()
            ctx.config_watcher = MagicMock()
            return 0.0

        with patch(
            "src.core.startup.StartupOrchestrator.run_all",
            _fake_run_all,
        ):
            await app._startup()

        assert app._phase == AppPhase.RUNNING

    async def test_calls_orchestrator_run_all(self, test_config: Config) -> None:
        app = Application(test_config)

        async def _populate_ctx_and_return():
            # Access the orchestrator's context to populate required components
            # The mock replaces the bound method, so we patch ctx after creation
            return 42.0

        mock_run = AsyncMock(side_effect=_populate_ctx_and_return)
        with patch.object(
            StartupOrchestrator,
            "run_all",
            mock_run,
        ):
            # Patch _build_state_from_ctx to avoid component validation
            # since the mock orchestrator doesn't populate the context
            with patch.object(
                Application,
                "_build_state_from_ctx",
                return_value=_make_mock_app_components(),
            ):
                await app._startup()

        mock_run.assert_awaited_once()

    async def test_returns_startup_time(self, test_config: Config) -> None:
        app = Application(test_config)

        async def _fake_run_all(self_orch):
            ctx = self_orch._ctx
            ctx.shutdown_mgr = MagicMock()
            ctx.components = _make_mock_bot_components()
            ctx.scheduler = _make_mock_scheduler()
            ctx.channel = _make_mock_channel()
            ctx.pipeline = MagicMock()
            ctx.executor = MagicMock(spec=ThreadPoolExecutor)
            ctx.workspace_monitor = MagicMock()
            ctx.config_watcher = MagicMock()
            return 100.0

        with patch(
            "src.core.startup.StartupOrchestrator.run_all",
            _fake_run_all,
        ):
            result = await app._startup()

        assert result == 100.0

    async def test_sets_health_server_from_ctx(self, test_config: Config) -> None:
        app = Application(test_config)
        mock_health = MagicMock()

        async def _fake_run_all(self_orch):
            # Populate the context so _build_state_from_ctx succeeds
            ctx = self_orch._ctx
            ctx.shutdown_mgr = MagicMock()
            ctx.components = _make_mock_bot_components()
            ctx.scheduler = _make_mock_scheduler()
            ctx.channel = _make_mock_channel()
            ctx.pipeline = MagicMock()
            ctx.executor = MagicMock(spec=ThreadPoolExecutor)
            ctx.workspace_monitor = MagicMock()
            ctx.config_watcher = MagicMock()
            ctx.health_server = mock_health
            return 0.0

        with patch("src.core.startup.StartupOrchestrator.run_all", _fake_run_all):
            await app._startup()

        assert app._health_server is mock_health

    async def test_populates_app_state(self, test_config: Config) -> None:
        app = Application(test_config)
        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        async def _fake_run_all(self_orch):
            ctx = self_orch._ctx
            ctx.shutdown_mgr = MagicMock()
            ctx.components = mock_components
            ctx.scheduler = mock_scheduler
            ctx.channel = mock_channel
            ctx.pipeline = MagicMock()
            ctx.executor = MagicMock(spec=ThreadPoolExecutor)
            ctx.workspace_monitor = MagicMock()
            ctx.config_watcher = MagicMock()
            return 0.0

        with patch("src.core.startup.StartupOrchestrator.run_all", _fake_run_all):
            await app._startup()

        assert app._state is not None
        assert app._state.components is mock_components
        assert app._state.channel is mock_channel
        assert app._state.scheduler is mock_scheduler

    async def test_state_is_frozen_app_components(self, test_config: Config) -> None:
        app = Application(test_config)

        async def _fake_run_all(self_orch):
            ctx = self_orch._ctx
            ctx.shutdown_mgr = MagicMock()
            ctx.components = _make_mock_bot_components()
            ctx.scheduler = _make_mock_scheduler()
            ctx.channel = _make_mock_channel()
            ctx.pipeline = MagicMock()
            ctx.executor = MagicMock(spec=ThreadPoolExecutor)
            ctx.workspace_monitor = MagicMock()
            ctx.config_watcher = MagicMock()
            return 0.0

        with patch("src.core.startup.StartupOrchestrator.run_all", _fake_run_all):
            await app._startup()

        assert isinstance(app._state, AppComponents)
        # Frozen dataclass — attribute assignment should raise
        with pytest.raises(AttributeError):
            app._state.pipeline = MagicMock()  # type: ignore[misc]

    async def test_health_server_none_when_no_port(self, test_config: Config) -> None:
        app = Application(test_config)  # no --health-port

        async def _fake_run_all(self_orch):
            ctx = self_orch._ctx
            ctx.shutdown_mgr = MagicMock()
            ctx.components = _make_mock_bot_components()
            ctx.scheduler = _make_mock_scheduler()
            ctx.channel = _make_mock_channel()
            ctx.pipeline = MagicMock()
            ctx.executor = MagicMock(spec=ThreadPoolExecutor)
            ctx.workspace_monitor = MagicMock()
            ctx.config_watcher = MagicMock()
            # health_server stays None
            return 0.0

        with patch("src.core.startup.StartupOrchestrator.run_all", _fake_run_all):
            await app._startup()

        assert app._health_server is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _wire_scheduler()
# ─────────────────────────────────────────────────────────────────────────────


class TestWireScheduler:
    """Tests for Application._wire_scheduler() callback wiring."""

    async def test_set_on_send_calls_channel_send_with_skip_delays(
        self, test_config: Config
    ) -> None:
        mock_bot = AsyncMock()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        Application._wire_scheduler(
            channel=mock_channel,
            bot=mock_bot,
            scheduler=mock_scheduler,
        )

        on_send = mock_scheduler.set_on_send.call_args[0][0]
        await on_send("chat-1", "hello")

        mock_channel.send_message.assert_awaited_once_with("chat-1", "hello", skip_delays=True)

    async def test_set_on_trigger_calls_bot_process_scheduled(self, test_config: Config) -> None:
        mock_bot = AsyncMock()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()

        Application._wire_scheduler(
            channel=mock_channel,
            bot=mock_bot,
            scheduler=mock_scheduler,
        )

        on_trigger = mock_scheduler.set_on_trigger.call_args[0][0]
        await on_trigger("chat-1", "Summarize today's events", None)

        mock_bot.process_scheduled.assert_awaited_once_with(
            "chat-1",
            "Summarize today's events",
            channel=mock_channel,
            prompt_hmac=None,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _on_message() — thin-wrapper delegation
# ─────────────────────────────────────────────────────────────────────────────


class TestOnMessageDelegation:
    """Tests that _on_message() delegates to pipeline.execute(ctx)."""

    async def test_calls_pipeline_execute(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()

        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True

        state = _make_mock_app_components(
            pipeline=mock_pipeline,
            shutdown_mgr=mock_shutdown,
        )
        app._state = state

        await app._on_message(msg)

        mock_pipeline.execute.assert_awaited_once()
        call_args = mock_pipeline.execute.call_args[0]
        assert call_args[0].msg is msg

    async def test_rejects_during_shutdown(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock()

        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = False

        state = _make_mock_app_components(
            pipeline=mock_pipeline,
            shutdown_mgr=mock_shutdown,
        )
        app._state = state

        await app._on_message(msg)

        mock_pipeline.execute.assert_not_called()

    async def test_propagates_pipeline_exception(self, test_config: Config) -> None:
        app = Application(test_config)
        msg = _make_msg()

        mock_pipeline = MagicMock()
        mock_pipeline.execute = AsyncMock(side_effect=RuntimeError("pipeline failed"))

        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True

        state = _make_mock_app_components(
            pipeline=mock_pipeline,
            shutdown_mgr=mock_shutdown,
        )
        app._state = state

        with pytest.raises(RuntimeError, match="pipeline failed"):
            await app._on_message(msg)


class TestOnMessageConcurrency:
    """Tests for the bounded concurrency semaphore in _on_message()."""

    async def test_semaphore_limits_concurrency(self, test_config: Config) -> None:
        """At most max_concurrent_messages are processed simultaneously."""
        test_config.max_concurrent_messages = 2
        app = Application(test_config)

        mock_shutdown = MagicMock()
        mock_shutdown.accepting_messages = True

        in_flight = 0
        max_in_flight = 0
        block = asyncio.Event()

        async def _track_execute(ctx: Any) -> None:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            if in_flight > max_in_flight:
                max_in_flight = in_flight
            await block.wait()
            in_flight -= 1

        mock_pipeline = MagicMock()
        mock_pipeline.execute = _track_execute

        state = _make_mock_app_components(
            pipeline=mock_pipeline,
            shutdown_mgr=mock_shutdown,
        )
        app._state = state

        # Launch 5 messages — only 2 should run at once.
        tasks = [
            asyncio.create_task(app._on_message(_make_msg(message_id=f"msg-{i}"))) for i in range(5)
        ]
        await asyncio.sleep(0.05)  # Let them hit the semaphore.

        assert max_in_flight <= 2, f"Expected ≤2 concurrent, got {max_in_flight}"

        block.set()
        await asyncio.gather(*tasks)

        assert max_in_flight == 2

    async def test_semaphore_default_value(self, test_config: Config) -> None:
        """Default semaphore is initialised from config.max_concurrent_messages."""
        from src.constants import DEFAULT_MAX_CONCURRENT_MESSAGES

        app = Application(test_config)
        assert app._message_semaphore._value == test_config.max_concurrent_messages
        assert test_config.max_concurrent_messages == DEFAULT_MAX_CONCURRENT_MESSAGES

    async def test_rejects_after_acquiring_semaphore_during_shutdown(
        self, test_config: Config
    ) -> None:
        """Messages that were queued at the semaphore are rejected if shutdown starts."""
        test_config.max_concurrent_messages = 1
        app = Application(test_config)

        mock_shutdown = MagicMock()
        block = asyncio.Event()
        call_count = 0

        async def _slow_execute(ctx: Any) -> None:
            nonlocal call_count
            call_count += 1
            await block.wait()

        mock_pipeline = MagicMock()
        mock_pipeline.execute = _slow_execute

        mock_shutdown.accepting_messages = True
        state = _make_mock_app_components(
            pipeline=mock_pipeline,
            shutdown_mgr=mock_shutdown,
        )
        app._state = state

        # First message occupies the single semaphore slot.
        first = asyncio.create_task(app._on_message(_make_msg(message_id="msg-1")))
        await asyncio.sleep(0.05)

        # Second message queues at the semaphore.
        second = asyncio.create_task(app._on_message(_make_msg(message_id="msg-2")))

        # Now trigger shutdown while second is queued.
        mock_shutdown.accepting_messages = False
        await asyncio.sleep(0.05)

        # Unblock the first message.
        block.set()
        await asyncio.sleep(0.05)

        # First message ran; second was rejected after acquiring the semaphore.
        assert call_count == 1
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass
        await first


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _shutdown_cleanup()
# ─────────────────────────────────────────────────────────────────────────────


class TestShutdownCleanup:
    """Tests for Application._shutdown_cleanup() delegating to perform_shutdown."""

    async def test_calls_perform_shutdown_with_all_components(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()
        mock_executor = MagicMock(spec=ThreadPoolExecutor)

        state = AppComponents(
            shutdown_mgr=mock_shutdown,
            components=mock_components,
            scheduler=mock_scheduler,
            channel=mock_channel,
            pipeline=MagicMock(),
            executor=mock_executor,
            workspace_monitor=MagicMock(),
            config_watcher=MagicMock(),
        )
        app._state = state
        app._health_server = None
        app._phase = AppPhase.RUNNING

        with patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps:
            await app._shutdown_cleanup()

        mock_ps.assert_awaited_once()
        ctx = mock_ps.call_args[0][0]
        assert ctx.shutdown is mock_shutdown
        assert ctx.channel is mock_channel
        assert ctx.scheduler is mock_scheduler
        assert ctx.db is mock_components.db
        assert ctx.vector_memory is mock_components.vector_memory
        assert ctx.project_store is mock_components.project_store
        assert ctx.message_queue is mock_components.message_queue
        assert ctx.llm is mock_components.llm
        assert ctx.bot is mock_components.bot
        assert ctx.executor is mock_executor
        assert ctx.session_metrics is not None

    async def test_passes_health_server(self, test_config: Config) -> None:
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()
        mock_health = MagicMock()

        state = AppComponents(
            shutdown_mgr=mock_shutdown,
            components=mock_components,
            scheduler=mock_scheduler,
            channel=mock_channel,
            pipeline=MagicMock(),
            executor=MagicMock(spec=ThreadPoolExecutor),
            workspace_monitor=MagicMock(),
            config_watcher=MagicMock(),
        )
        app._state = state
        app._health_server = mock_health
        app._phase = AppPhase.RUNNING

        with patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps:
            await app._shutdown_cleanup()

        ctx = mock_ps.call_args[0][0]
        assert ctx.health_server is mock_health

    async def test_config_watcher_timeout_does_not_block_cleanup(self, test_config: Config) -> None:
        """A hung config_watcher.stop() is cancelled after the step timeout."""
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()

        # config_watcher.stop() never resolves
        mock_cw = MagicMock()

        async def _hang_forever():
            await asyncio.Event().wait()

        mock_cw.stop = AsyncMock(side_effect=_hang_forever)
        mock_wm = MagicMock()
        mock_wm.stop = AsyncMock()

        state = AppComponents(
            shutdown_mgr=mock_shutdown,
            components=mock_components,
            scheduler=mock_scheduler,
            channel=mock_channel,
            pipeline=MagicMock(),
            executor=MagicMock(spec=ThreadPoolExecutor),
            workspace_monitor=mock_wm,
            config_watcher=mock_cw,
        )
        app._state = state
        app._health_server = None
        app._phase = AppPhase.RUNNING

        with (
            patch("src.app.CLEANUP_STEP_TIMEOUT", 0.05),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps,
            patch("src.app.log_noncritical") as mock_log_nc,
        ):
            await app._shutdown_cleanup()

        # perform_shutdown still called despite config watcher timeout
        mock_ps.assert_awaited_once()

        # Verify structured shutdown log emitted
        mock_log_nc.assert_called_once()
        call_kwargs = mock_log_nc.call_args
        assert call_kwargs[1]["extra"]["shutdown_step"] == "config_watcher_stop"
        assert call_kwargs[1]["extra"]["timeout_seconds"] == 0.05
        assert call_kwargs[1]["extra"]["affected_components"] == ["config_watcher"]

    async def test_workspace_monitor_timeout_does_not_block_cleanup(
        self, test_config: Config
    ) -> None:
        """A hung workspace_monitor.stop() is cancelled after the step timeout."""
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()

        mock_cw = MagicMock()
        mock_cw.stop = AsyncMock()
        mock_wm = MagicMock()

        async def _hang_forever():
            await asyncio.Event().wait()

        mock_wm.stop = AsyncMock(side_effect=_hang_forever)

        state = AppComponents(
            shutdown_mgr=mock_shutdown,
            components=mock_components,
            scheduler=mock_scheduler,
            channel=mock_channel,
            pipeline=MagicMock(),
            executor=MagicMock(spec=ThreadPoolExecutor),
            workspace_monitor=mock_wm,
            config_watcher=mock_cw,
        )
        app._state = state
        app._health_server = None
        app._phase = AppPhase.RUNNING

        with (
            patch("src.app.CLEANUP_STEP_TIMEOUT", 0.05),
            patch("src.app.perform_shutdown", new_callable=AsyncMock) as mock_ps,
            patch("src.app.log_noncritical") as mock_log_nc,
        ):
            await app._shutdown_cleanup()

        mock_ps.assert_awaited_once()

        # Verify structured shutdown log emitted
        mock_log_nc.assert_called_once()
        call_kwargs = mock_log_nc.call_args
        assert call_kwargs[1]["extra"]["shutdown_step"] == "workspace_monitor_stop"
        assert call_kwargs[1]["extra"]["timeout_seconds"] == 0.05
        assert call_kwargs[1]["extra"]["affected_components"] == ["workspace_monitor"]

    async def test_perform_shutdown_timeout_still_transitions_to_stopped(
        self, test_config: Config
    ) -> None:
        """If perform_shutdown times out, we still transition to STOPPED."""
        app = Application(test_config)

        mock_components = _make_mock_bot_components()
        mock_channel = _make_mock_channel()
        mock_scheduler = _make_mock_scheduler()
        mock_shutdown = MagicMock()

        mock_cw = MagicMock()
        mock_cw.stop = AsyncMock()
        mock_wm = MagicMock()
        mock_wm.stop = AsyncMock()

        state = AppComponents(
            shutdown_mgr=mock_shutdown,
            components=mock_components,
            scheduler=mock_scheduler,
            channel=mock_channel,
            pipeline=MagicMock(),
            executor=MagicMock(spec=ThreadPoolExecutor),
            workspace_monitor=mock_wm,
            config_watcher=mock_cw,
        )
        app._state = state
        app._health_server = None
        app._phase = AppPhase.RUNNING

        async def _hang_forever(*_args, **_kwargs):
            await asyncio.Event().wait()

        with (
            patch("src.app.CLEANUP_STEP_TIMEOUT", 0.05),
            patch("src.app.perform_shutdown", side_effect=_hang_forever) as mock_ps,
            patch("src.app.log_noncritical") as mock_log_nc,
        ):
            await app._shutdown_cleanup()

        mock_ps.assert_awaited_once()
        assert app._phase == AppPhase.STOPPED

        # Verify structured shutdown log emitted
        mock_log_nc.assert_called_once()
        call_kwargs = mock_log_nc.call_args
        assert call_kwargs[1]["extra"]["shutdown_step"] == "perform_shutdown"
        assert call_kwargs[1]["extra"]["timeout_seconds"] == 0.05
        assert "channel" in call_kwargs[1]["extra"]["affected_components"]
        assert "db" in call_kwargs[1]["extra"]["affected_components"]
        assert "bot" in call_kwargs[1]["extra"]["affected_components"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Property accessors (pre-startup guards)
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _transition() phase validation
# ─────────────────────────────────────────────────────────────────────────────


class TestPhaseTransitions:
    """Tests for Application._transition() validating phase transitions."""

    def test_invalid_created_to_stopped(self, test_config: Config) -> None:
        app = Application(test_config)
        assert app._phase == AppPhase.CREATED

        with pytest.raises(RuntimeError, match="Invalid phase transition"):
            app._transition(AppPhase.STOPPED)

        # Phase unchanged after rejection
        assert app._phase == AppPhase.CREATED

    def test_invalid_created_to_running(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Invalid phase transition"):
            app._transition(AppPhase.RUNNING)

    def test_invalid_running_to_starting(self, test_config: Config) -> None:
        app = Application(test_config)
        app._phase = AppPhase.RUNNING
        with pytest.raises(RuntimeError, match="Invalid phase transition"):
            app._transition(AppPhase.STARTING)

    def test_invalid_running_to_stopped(self, test_config: Config) -> None:
        app = Application(test_config)
        app._phase = AppPhase.RUNNING
        with pytest.raises(RuntimeError, match="Invalid phase transition"):
            app._transition(AppPhase.STOPPED)

    def test_invalid_stopped_to_any(self, test_config: Config) -> None:
        """STOPPED is terminal — no transitions out."""
        app = Application(test_config)
        app._phase = AppPhase.STOPPED
        for target in AppPhase:
            if target == AppPhase.STOPPED:
                continue
            with pytest.raises(RuntimeError, match="Invalid phase transition"):
                app._transition(target)

    def test_valid_full_lifecycle_sequence(self, test_config: Config) -> None:
        """CREATED → STARTING → RUNNING → SHUTTING_DOWN → STOPPED succeeds."""
        app = Application(test_config)
        assert app._phase == AppPhase.CREATED

        app._transition(AppPhase.STARTING)
        assert app._phase == AppPhase.STARTING

        app._transition(AppPhase.RUNNING)
        assert app._phase == AppPhase.RUNNING

        app._transition(AppPhase.SHUTTING_DOWN)
        assert app._phase == AppPhase.SHUTTING_DOWN

        app._transition(AppPhase.STOPPED)
        assert app._phase == AppPhase.STOPPED

    def test_valid_starting_to_shutting_down(self, test_config: Config) -> None:
        """STARTING may go to SHUTTING_DOWN (startup failure path)."""
        app = Application(test_config)
        app._phase = AppPhase.STARTING
        app._transition(AppPhase.SHUTTING_DOWN)
        assert app._phase == AppPhase.SHUTTING_DOWN

    def test_error_message_includes_phase_names(self, test_config: Config) -> None:
        """RuntimeError message contains both the source and target phase names."""
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="CREATED.*STOPPED"):
            app._transition(AppPhase.STOPPED)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Startup rollback on failure
# ─────────────────────────────────────────────────────────────────────────────


class TestStartupRollback:
    """Tests for Application rollback during startup failure.

    When a component spec raises during ``_startup()``, the Application
    is left in STARTING phase with ``_state = None``.  These tests verify
    that the state machine allows the STARTING → SHUTTING_DOWN → STOPPED
    rollback path and that ``_shutdown_cleanup()`` handles the
    partially-initialised state without AttributeError.
    """

    async def test_full_rollback_transition_sequence(
        self, test_config: Config
    ) -> None:
        """The state machine permits STARTING → SHUTTING_DOWN → STOPPED."""
        app = Application(test_config)
        app._transition(AppPhase.STARTING)
        assert app._phase == AppPhase.STARTING

        # Startup fails — rollback through SHUTTING_DOWN to STOPPED
        app._transition(AppPhase.SHUTTING_DOWN)
        assert app._phase == AppPhase.SHUTTING_DOWN

        app._transition(AppPhase.STOPPED)
        assert app._phase == AppPhase.STOPPED

    async def test_shutdown_cleanup_from_starting_phase(
        self, test_config: Config
    ) -> None:
        """_shutdown_cleanup() during STARTING with _state=None completes
        without AttributeError from partially-initialised components."""
        app = Application(test_config)
        app._transition(AppPhase.STARTING)
        assert app._state is None

        # No crash — _state is None, so cleanup returns early
        await app._shutdown_cleanup()

        # Phase unchanged: _shutdown_cleanup only transitions from RUNNING,
        # and returns early when _state is None
        assert app._phase == AppPhase.STARTING

    async def test_shutdown_cleanup_after_manual_shutting_down_no_state(
        self, test_config: Config
    ) -> None:
        """When caller transitions to SHUTTING_DOWN after startup failure,
        _shutdown_cleanup() returns early without AttributeError."""
        app = Application(test_config)
        app._transition(AppPhase.STARTING)
        app._transition(AppPhase.SHUTTING_DOWN)
        assert app._state is None

        await app._shutdown_cleanup()

        # _state is None → early return, never reaches the STOPPED transition
        assert app._phase == AppPhase.SHUTTING_DOWN


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Property accessors (pre-startup guards)
# ─────────────────────────────────────────────────────────────────────────────


class TestPropertyGuards:
    """Tests that property accessors raise before _startup() is called."""

    def test_channel_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Components not available in CREATED phase"):
            _ = app.channel

    def test_components_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Components not available in CREATED phase"):
            _ = app.components

    def test_shutdown_mgr_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Components not available in CREATED phase"):
            _ = app.shutdown_mgr

    def test_scheduler_raises_before_startup(self, test_config: Config) -> None:
        app = Application(test_config)
        with pytest.raises(RuntimeError, match="Components not available in CREATED phase"):
            _ = app.scheduler
