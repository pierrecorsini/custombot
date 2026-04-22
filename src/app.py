"""
src/app.py — Application lifecycle manager.

Encapsulates the full bot application lifecycle in a single class,
with named methods for each phase: startup, wiring, message handling,
and shutdown. Makes the startup sequence testable without a running
WhatsApp connection.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from src.builder import BotComponents, _build_bot
from src.channels.base import BaseChannel, IncomingMessage
from src.config import Config, CONFIG_PATH
from src.config.config_watcher import ConfigChangeApplier, ConfigWatcher
from src.constants import CONFIG_WATCH_INTERVAL_SECONDS, DEFAULT_CHANNEL_STARTUP_TIMEOUT, DEFAULT_SHUTDOWN_TIMEOUT, DEFAULT_THREAD_POOL_WORKERS, WORKSPACE_CLEANUP_INTERVAL_SECONDS, WORKSPACE_DIR
from src.core.event_bus import EVENT_ERROR_OCCURRED, Event, get_event_bus
from src.core.message_pipeline import (
    MessageContext,
    MessagePipeline,
    PipelineDependencies,
    build_pipeline_from_config,
)
from src.lifecycle import (
    _log_component_init,
    _log_component_ready,
    _log_startup_begin,
    _log_startup_complete,
    perform_shutdown,
)
from src.logging.logging_config import (
    clear_correlation_id,
    get_correlation_id,
    set_correlation_id,
)
from src.monitoring import SessionMetrics
from src.monitoring.workspace_monitor import WorkspaceMonitor
from src.scheduler import TaskScheduler
from src.shutdown import GracefulShutdown
from src.skills.builtin.task_scheduler import set_scheduler_instance
from src.ui.cli_output import cli as cli_output

if TYPE_CHECKING:
    from src.health import HealthServer

log = logging.getLogger(__name__)


class _NoOpConfigApplier:
    """Fallback applier used when the channel is not a WhatsAppChannel (e.g. tests).

    Still detects and logs config changes but does not propagate them to
    components that require a concrete channel type.
    """

    def apply(self, old_config: Config, new_config: Config) -> None:
        log.debug("Config change detected but channel is mocked — skipping apply")


class Application:
    """Encapsulates the full bot application lifecycle.

    Phases:
        1. ``_startup()`` — Initialize all components
        2. ``_wire_scheduler()`` — Configure scheduler callbacks
        3. ``_on_message()`` — Handle incoming messages
        4. ``run()`` — Start polling, wait for shutdown, then cleanup
    """

    def __init__(
        self,
        config: Config,
        verbose: bool = False,
        health_port: Optional[int] = None,
        safe_mode: bool = False,
    ) -> None:
        self._config = config
        self._verbose = verbose
        self._health_port = health_port
        self._safe_mode = safe_mode
        self._session_metrics = SessionMetrics()
        self._initialized_components: list[str] = []

        # Components — set during _startup()
        self._shutdown_mgr: GracefulShutdown | None = None
        self._components: BotComponents | None = None
        self._scheduler: TaskScheduler | None = None
        self._channel: BaseChannel | None = None
        self._pipeline: MessagePipeline | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._workspace_monitor: WorkspaceMonitor | None = None
        self._config_watcher: ConfigWatcher | None = None

    @property
    def channel(self) -> BaseChannel:
        """The initialized channel. Raises if called before ``_startup()``."""
        if self._channel is None:
            raise RuntimeError("Channel not initialized. Call _startup() first.")
        return self._channel

    @property
    def components(self) -> BotComponents:
        """The initialized bot components. Raises if called before ``_startup()``."""
        if self._components is None:
            raise RuntimeError("Components not initialized. Call _startup() first.")
        return self._components

    @property
    def shutdown_mgr(self) -> GracefulShutdown:
        """The shutdown manager. Raises if called before ``_startup()``."""
        if self._shutdown_mgr is None:
            raise RuntimeError("Shutdown manager not initialized. Call _startup() first.")
        return self._shutdown_mgr

    @property
    def scheduler(self) -> TaskScheduler:
        """The task scheduler. Raises if called before ``_startup()``."""
        if self._scheduler is None:
            raise RuntimeError("Scheduler not initialized. Call _startup() first.")
        return self._scheduler

    # ── Public API ──────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Full lifecycle: startup → listen → shutdown."""
        startup_time = await self._startup()

        poll_task = asyncio.create_task(
            self.channel.start(self._on_message)
        )
        _log_component_init("Message Poller", "started")

        # Wait for either successful connection or early channel exit,
        # with a generous startup timeout.  If channel.start() hangs
        # (neonize bug, network issue) the timeout fires; if the channel
        # gives up (e.g. QR wait exceeded) we detect it immediately.
        connect_waiter = asyncio.create_task(self.channel.wait_connected())

        done, _pending = await asyncio.wait(
            [poll_task, connect_waiter],
            timeout=DEFAULT_CHANNEL_STARTUP_TIMEOUT,
            return_when=asyncio.FIRST_COMPLETED,
        )

        if connect_waiter not in done:
            # Connection not established — either timeout or channel exited.
            connect_waiter.cancel()
            if poll_task in done:
                exc = poll_task.exception()
                if exc:
                    log.error("Channel exited with error during startup: %s", exc)
                else:
                    log.error("Channel exited before establishing connection")
            else:
                log.error(
                    "Channel failed to connect within %.0f seconds — aborting startup",
                    DEFAULT_CHANNEL_STARTUP_TIMEOUT,
                )
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass
            await self._shutdown_cleanup()
            return

        _log_component_ready("Message Poller")
        self._initialized_components.append("Message Poller")

        _log_startup_complete(startup_time, self._initialized_components, self.components.component_durations)

        try:
            cli_output.info("Listening...  (Ctrl+C to stop)")
            await self.shutdown_mgr.wait_for_shutdown()
        except Exception as e:
            log.error("Unexpected error in main loop: %s", e, exc_info=self._verbose)
            self._session_metrics.increment_errors()
            from src.monitoring.performance import get_metrics_collector
            get_metrics_collector().track_error()
        finally:
            await self._shutdown_cleanup()

    # ── Startup Phase ───────────────────────────────────────────────────────

    async def _startup(self) -> float:
        """Initialize all components and return the startup begin timestamp."""
        from src.channels.whatsapp import WhatsAppChannel

        startup_time = _log_startup_begin(self._config)

        # Shutdown manager
        _log_component_init("Shutdown Manager", "started")
        timeout = (
            self._config.shutdown_timeout
            if self._config.shutdown_timeout is not None
            else DEFAULT_SHUTDOWN_TIMEOUT
        )
        self._shutdown_mgr = GracefulShutdown(timeout=timeout)
        loop = asyncio.get_running_loop()
        self.shutdown_mgr.register_signal_handlers(loop)
        _log_component_ready("Shutdown Manager")
        self._initialized_components.append("Shutdown Manager")

        # Thread pool executor — must be set BEFORE _build_bot() so all
        # subsequent asyncio.to_thread() calls use the configured pool.
        _log_component_init("Thread Pool Executor", "started")
        workers = self._config.max_thread_pool_workers or DEFAULT_THREAD_POOL_WORKERS
        self._executor = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="cb-worker"
        )
        loop.set_default_executor(self._executor)
        _log_component_ready("Thread Pool Executor", f"max_workers={workers}")
        self._initialized_components.append(f"Thread Pool ({workers} workers)")

        # Bot components
        _log_component_init("Bot Components", "started")
        self._components = await _build_bot(self._config, session_metrics=self._session_metrics)
        self.components.bot.validate_wiring()
        _log_component_ready("Bot Components", "all subsystems ready")
        self._initialized_components.append("Bot (LLM, Memory, Skills, Routing)")
        self._initialized_components.append("Database")

        # Scheduler
        self._scheduler = await self._init_scheduler()

        # Channel
        _log_component_init("WhatsApp Channel", "started")
        self._channel = WhatsAppChannel(
            self._config.whatsapp,
            safe_mode=self._safe_mode,
            load_history=self._config.load_history,
        )
        _log_component_ready(
            "WhatsApp Channel", f"provider={self._config.whatsapp.provider}"
        )
        self._initialized_components.append("WhatsApp Channel")

        self._wire_scheduler()
        await self._start_health_server()

        # Recover stale messages from previous crash/restart
        await self.components.bot.recover_pending_messages(channel=self.channel)

        self._pipeline = self._build_pipeline()

        # Start workspace size monitoring and periodic cleanup
        _log_component_init("Workspace Monitor", "started")
        self._workspace_monitor = WorkspaceMonitor(workspace_dir=WORKSPACE_DIR)
        self._workspace_monitor.start_periodic_cleanup()
        _log_component_ready(
            "Workspace Monitor",
            f"interval={WORKSPACE_CLEANUP_INTERVAL_SECONDS:.0f}s",
        )
        self._initialized_components.append("Workspace Monitor")

        # Start config hot-reload watcher (polling-based)
        _log_component_init("Config Watcher", "started")
        self._config_watcher = self._init_config_watcher()
        self._config_watcher.start()
        _log_component_ready(
            "Config Watcher",
            f"path={CONFIG_PATH}, interval={CONFIG_WATCH_INTERVAL_SECONDS:.0f}s",
        )
        self._initialized_components.append("Config Watcher")

        return startup_time

    async def _init_scheduler(self) -> TaskScheduler:
        """Create, configure, and start the task scheduler."""
        _log_component_init("Task Scheduler", "started")
        workspace = Path(WORKSPACE_DIR)
        scheduler = TaskScheduler()
        # Wire unified dedup service for outbound message dedup
        scheduler.set_dedup_service(self.components.dedup)
        scheduler.configure(
            workspace=workspace,
            on_trigger=lambda chat_id, prompt: self.components.bot.process_scheduled(
                chat_id, prompt
            ),
        )
        set_scheduler_instance(scheduler)
        await scheduler.load_all()
        scheduler.start()
        _log_component_ready("Task Scheduler", f"workspace={workspace}")
        self._initialized_components.append("Task Scheduler")
        return scheduler

    def _init_config_watcher(self) -> ConfigWatcher:
        """Create the config hot-reload watcher with component references."""
        from src.channels.whatsapp import WhatsAppChannel

        # Only set up hot-reload with a real WhatsAppChannel (not test mocks)
        if not isinstance(self._channel, WhatsAppChannel):
            log.debug(
                "Config watcher: channel is not WhatsAppChannel — using no-op applier"
            )
            applier = _NoOpConfigApplier()
        else:
            applier = ConfigChangeApplier(
                app_config=self._config,
                bot=self.components.bot,
                channel=self._channel,
                llm=self.components.llm,
                shutdown_mgr=self.shutdown_mgr,
                reconfigure_logging=self._reconfigure_logging,
            )
        return ConfigWatcher(
            config_path=CONFIG_PATH,
            current_config=self._config,
            applier=applier,
        )

    @staticmethod
    def _reconfigure_logging(config: Config) -> None:
        """Reconfigure logging after a config change (e.g. verbosity)."""
        from main import _setup_logging

        _setup_logging(
            verbose=False,
            log_format=config.log_format,
            log_file=config.log_file,
            log_max_bytes=config.log_max_bytes,
            log_backup_count=config.log_backup_count,
            log_verbosity=config.log_verbosity,
        )

    # ── Pipeline Construction ────────────────────────────────────────────────

    def _build_pipeline(self) -> MessagePipeline:
        """Build the message-processing middleware chain from config.

        Uses ``build_pipeline_from_config()`` so the middleware order and
        custom middleware paths are driven by ``config.json``.  Falls back
        to the built-in default order when ``middleware_order`` is empty.
        """
        mw_cfg = self._config.middleware
        deps = PipelineDependencies(
            shutdown_mgr=self.shutdown_mgr,
            session_metrics=self._session_metrics,
            bot=self.components.bot,
            channel=self.channel,
            verbose=self._verbose,
        )
        return build_pipeline_from_config(
            middleware_order=mw_cfg.middleware_order,
            extra_middleware_paths=mw_cfg.extra_middleware_paths,
            deps=deps,
        )

    # ── Wiring Phase ────────────────────────────────────────────────────────

    def _wire_scheduler(self) -> None:
        """Wire scheduler callbacks to the WhatsApp channel."""
        channel = self.channel
        bot = self.components.bot

        # skip_delays=True bypasses human-like stealth delays for scheduled messages
        self.scheduler.set_on_send(
            lambda chat_id, text: channel.send_message(chat_id, text, skip_delays=True)
        )

        self.scheduler.set_on_trigger(
            lambda chat_id, prompt: bot.process_scheduled(
                chat_id, prompt, channel=channel
            )
        )

    async def _start_health_server(self) -> None:
        """Start health check server if a port was configured."""
        if not self._health_port:
            return

        from src.health import HealthServer

        _log_component_init("Health Server", "started")
        try:
            self._health_server = HealthServer(
                db=self.components.db,
                token_usage=self.components.token_usage,
                bot=self.components.bot,
                scheduler=self.scheduler,
                llm_log_dir=(
                    f"{WORKSPACE_DIR}/logs/llm" if self._config.log_llm else None
                ),
                workspace_dir=WORKSPACE_DIR,
                shutdown_mgr=self.shutdown_mgr,
            )
            await self._health_server.start(port=self._health_port)
            _log_component_ready("Health Server", f"port={self._health_port}")
            self._initialized_components.append(f"Health Server (port {self._health_port})")
            cli_output.dim(
                f"  Health check: http://0.0.0.0:{self._health_port}/health  "
                f"Readiness: http://0.0.0.0:{self._health_port}/ready"
            )
        except Exception as e:
            log.warning("Failed to start health server on port %d: %s", self._health_port, e)
            self._health_server = None

    # ── Message Handler ─────────────────────────────────────────────────────

    async def _on_message(self, msg: IncomingMessage) -> None:
        """Handle incoming message via the middleware pipeline."""
        if not self.shutdown_mgr.accepting_messages:
            log.debug("Rejecting message from %s - shutdown in progress", msg.chat_id)
            return

        assert self._pipeline is not None  # set during _startup()

        # Propagate correlation ID from the incoming message (or generate a
        # fresh one) so that all downstream logging and event emission can be
        # traced back to this message.
        set_correlation_id(msg.correlation_id)

        ctx = MessageContext(msg=msg)
        try:
            await self._pipeline.execute(ctx)
        except Exception as exc:
            # Emit an error_occurred event so that monitoring subscribers are
            # notified of pipeline failures.  Event emission itself must never
            # break the error-handling path.
            try:
                await get_event_bus().emit(Event(
                    name=EVENT_ERROR_OCCURRED,
                    data={
                        "chat_id": msg.chat_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                    source="Application._on_message",
                    correlation_id=get_correlation_id(),
                ))
            except Exception:
                pass  # Event emission must not mask the original exception
            raise
        finally:
            clear_correlation_id()

    # ── Shutdown Phase ──────────────────────────────────────────────────────

    async def _shutdown_cleanup(self) -> None:
        """Delegate to the shared ordered-shutdown sequence."""
        # Stop config watcher before general shutdown
        if self._config_watcher is not None:
            try:
                await self._config_watcher.stop()
            except Exception as e:
                log.warning("Error stopping config watcher: %s", e)

        # Stop workspace monitor before general shutdown
        if self._workspace_monitor is not None:
            try:
                await self._workspace_monitor.stop()
            except Exception as e:
                log.warning("Error stopping workspace monitor: %s", e)

        await perform_shutdown(
            shutdown=self.shutdown_mgr,
            channel=self.channel,
            scheduler=self.scheduler,
            health_server=self._health_server,
            db=self.components.db,
            vector_memory=self.components.vector_memory,
            project_store=self.components.project_store,
            message_queue=self.components.message_queue,
            llm=self.components.llm,
            session_metrics=self._session_metrics.to_dict(),
            log=log,
            verbose=self._verbose,
            bot=self.components.bot,
            executor=self._executor,
        )
