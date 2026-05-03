"""
src/app.py — Application lifecycle manager.

Encapsulates the full bot application lifecycle in a single class,
with named methods for each phase: startup, wiring, message handling,
and shutdown. Makes the startup sequence testable without a running
WhatsApp connection.

Startup is delegated to ``StartupOrchestrator`` (see ``src/core/startup.py``)
which executes a declarative list of ``ComponentSpec`` steps in order.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Optional

from src.builder import BotComponents
from src.channels.base import BaseChannel, IncomingMessage
from src.config import Config
from src.constants import CLEANUP_STEP_TIMEOUT, DEFAULT_CHANNEL_STARTUP_TIMEOUT
from src.core.event_bus import EVENT_ERROR_OCCURRED, Event, get_event_bus
from src.core.message_pipeline import MessageContext, MessagePipeline
from src.core.startup import StartupContext, StartupOrchestrator
from src.core.errors import NonCriticalCategory, log_noncritical
from src.lifecycle import ShutdownContext, _log_component_init, _log_component_ready, _log_startup_complete, perform_shutdown
from src.logging.logging_config import (
    clear_correlation_id,
    get_correlation_id,
    set_correlation_id,
)
from src.monitoring import SessionMetrics
from src.ui.cli_output import cli as cli_output

if TYPE_CHECKING:
    from src.bot import Bot
    from src.config.config_watcher import ConfigWatcher
    from src.health import HealthServer
    from src.monitoring.workspace_monitor import WorkspaceMonitor
    from src.scheduler import TaskScheduler
    from src.shutdown import GracefulShutdown

log = logging.getLogger(__name__)


# ── Lifecycle State Machine ──────────────────────────────────────────────


class AppPhase(Enum):
    """Explicit lifecycle phases for the Application state machine.

    Transitions are validated by ``Application._transition()`` so that
    misuse (e.g. calling ``_on_message`` before startup) is caught with
    a clear error instead of a confusing ``AttributeError``.
    """

    CREATED = auto()
    STARTING = auto()
    RUNNING = auto()
    SHUTTING_DOWN = auto()
    STOPPED = auto()


@dataclass(frozen=True)
class AppComponents:
    """All components guaranteed non-None after successful startup.

    Constructed atomically from ``StartupContext`` after all startup
    steps complete — no partially-initialised state is possible.
    Because the dataclass is frozen, downstream code can trust that
    every field is populated without null-checks.
    """

    shutdown_mgr: GracefulShutdown
    components: BotComponents
    scheduler: TaskScheduler
    channel: BaseChannel
    pipeline: MessagePipeline
    executor: ThreadPoolExecutor
    workspace_monitor: WorkspaceMonitor
    config_watcher: ConfigWatcher


# ── Valid phase transitions ──────────────────────────────────────────────

_VALID_TRANSITIONS: dict[AppPhase, set[AppPhase]] = {
    AppPhase.CREATED: {AppPhase.STARTING},
    AppPhase.STARTING: {AppPhase.RUNNING, AppPhase.SHUTTING_DOWN},
    AppPhase.RUNNING: {AppPhase.SHUTTING_DOWN},
    AppPhase.SHUTTING_DOWN: {AppPhase.STOPPED},
}


# ── Main-loop error categorization ──────────────────────────────────────


class _MainLoopErrorCategory:
    """Category strings for structured main-loop error classification.

    Monitoring subscribers check ``event.data["category"]`` to trigger
    alerts or auto-recovery for specific failure modes.
    """

    LLM_TRANSIENT = "llm_transient"
    LLM_PERMANENT = "llm_permanent"
    CHANNEL_DISCONNECT = "channel_disconnect"
    FILESYSTEM = "filesystem"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


def _classify_main_loop_error(exc: Exception) -> str:
    """Classify a main-loop exception into a monitoring category.

    Uses the existing ``CustomBotException`` hierarchy and ``ErrorCode``
    enum to determine whether the error is transient (retryable),
    permanent, channel-related, filesystem-related, or configuration-related.
    """
    from src.exceptions import (
        BridgeError,
        ConfigurationError,
        DatabaseError,
        DiskSpaceError,
        ErrorCode,
        LLMError,
    )

    # LLM errors — distinguish transient from permanent via error code
    if isinstance(exc, LLMError):
        transient_codes = {
            ErrorCode.LLM_TIMEOUT,
            ErrorCode.LLM_CONNECTION_FAILED,
            ErrorCode.LLM_RATE_LIMITED,
            ErrorCode.LLM_CIRCUIT_BREAKER_OPEN,
        }
        if exc.error_code in transient_codes:
            return _MainLoopErrorCategory.LLM_TRANSIENT
        return _MainLoopErrorCategory.LLM_PERMANENT

    # Channel / bridge disconnection
    if isinstance(exc, (BridgeError, ConnectionError)):
        return _MainLoopErrorCategory.CHANNEL_DISCONNECT

    # Filesystem / database / disk
    if isinstance(exc, (DatabaseError, DiskSpaceError, OSError)):
        return _MainLoopErrorCategory.FILESYSTEM

    # Configuration issues
    if isinstance(exc, ConfigurationError):
        return _MainLoopErrorCategory.CONFIGURATION

    return _MainLoopErrorCategory.UNKNOWN


class Application:
    """Encapsulates the full bot application lifecycle.

    Phases:
        1. ``CREATED → STARTING`` — ``_startup()`` begins
        2. ``STARTING → RUNNING`` — all components initialised
        3. ``RUNNING → SHUTTING_DOWN`` — shutdown requested
        4. ``SHUTTING_DOWN → STOPPED`` — cleanup complete
    """

    def __init__(
        self,
        config: Config,
        verbose: bool = False,
        health_port: Optional[int] = None,
        health_host: str = "127.0.0.1",
        safe_mode: bool = False,
    ) -> None:
        self._config = config
        self._verbose = verbose
        self._health_port = health_port
        self._health_host = health_host
        self._safe_mode = safe_mode
        self._session_metrics = SessionMetrics()
        self._initialized_components: list[str] = []

        # State machine — components are only available after STARTING → RUNNING
        self._phase: AppPhase = AppPhase.CREATED
        self._state: AppComponents | None = None
        # Health server is optional (only created when --health-port is set)
        self._health_server: HealthServer | None = None

        # Bounded concurrency — caps concurrent message processing to avoid
        # exhausting memory and LLM rate limits under load.
        self._message_semaphore = asyncio.Semaphore(config.max_concurrent_messages)

    # ── Phase transitions ────────────────────────────────────────────────

    def _transition(self, new_phase: AppPhase) -> None:
        """Validate and apply a phase transition.

        Raises ``RuntimeError`` if the transition is not allowed from the
        current phase, making lifecycle misuse detectable at the call site.
        """
        allowed = _VALID_TRANSITIONS.get(self._phase, set())
        if new_phase not in allowed:
            raise RuntimeError(
                f"Invalid phase transition: {self._phase.name} → "
                f"{new_phase.name}. Current phase does not permit this transition."
            )
        log.debug("App phase: %s → %s", self._phase.name, new_phase.name)
        self._phase = new_phase

    @property
    def phase(self) -> AppPhase:
        """The current lifecycle phase (read-only for external consumers)."""
        return self._phase

    @property
    def state(self) -> AppComponents:
        """The initialised components. Raises if accessed before startup completes."""
        if self._state is None:
            raise RuntimeError(
                f"Components not available in {self._phase.name} phase. "
                "Startup must complete first."
            )
        return self._state

    # ── Component accessors ──────────────────────────────────────────────

    @property
    def channel(self) -> BaseChannel:
        """The initialised channel. Raises if called before startup completes."""
        return self.state.channel

    @property
    def components(self) -> BotComponents:
        """The initialised bot components. Raises if called before startup completes."""
        return self.state.components

    @property
    def shutdown_mgr(self) -> GracefulShutdown:
        """The shutdown manager. Raises if called before startup completes."""
        return self.state.shutdown_mgr

    @property
    def scheduler(self) -> TaskScheduler:
        """The task scheduler. Raises if called before startup completes."""
        return self.state.scheduler

    # ── Public API ──────────────────────────────────────────────────────

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
        except Exception as exc:
            category = _classify_main_loop_error(exc)
            log.error(
                "Unexpected error in main loop [%s]: %s",
                category, exc, exc_info=self._verbose,
            )
            self._session_metrics.increment_errors()
            from src.monitoring.performance import get_metrics_collector
            get_metrics_collector().track_error()

            # Emit structured error event for monitoring subscribers.
            try:
                await get_event_bus().emit(Event(
                    name=EVENT_ERROR_OCCURRED,
                    data={
                        "category": category,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                        "source": "main_loop",
                    },
                    source="Application.run",
                    correlation_id=get_correlation_id(),
                ))
            except Exception:
                log_noncritical(
                    NonCriticalCategory.EVENT_EMISSION,
                    "Failed to emit error_occurred event from main loop",
                    logger=log,
                )
        finally:
            await self._shutdown_cleanup()

    # ── Startup Phase ───────────────────────────────────────────────────

    async def _startup(self) -> float:
        """Initialize all components via ``StartupOrchestrator``.

        Transitions ``CREATED → STARTING``, runs all startup steps, then
        atomically constructs ``AppComponents`` from the context and
        transitions ``STARTING → RUNNING``.

        Returns the startup begin timestamp for duration tracking.
        """
        self._transition(AppPhase.STARTING)

        ctx = StartupContext(
            config=self._config,
            session_metrics=self._session_metrics,
            app=self,
        )
        orchestrator = StartupOrchestrator(ctx)
        startup_time = await orchestrator.run_all()

        # Atomically construct frozen state from the completed context.
        self._state = self._build_state_from_ctx(ctx)
        self._health_server = ctx.health_server

        # Update the health server with the complete startup durations
        # (the snapshot taken during _step_health_server only covers steps
        # that ran *before* the Health Server step).
        if self._health_server is not None:
            full_durations = dict(ctx.component_durations)
            if ctx.components is not None and ctx.components.component_durations:
                full_durations.update(ctx.components.component_durations)
            self._health_server.update_startup_durations(full_durations)

        self._transition(AppPhase.RUNNING)
        return startup_time

    @staticmethod
    def _build_state_from_ctx(ctx: StartupContext) -> AppComponents:
        """Construct ``AppComponents`` from a successfully completed ``StartupContext``.

        Delegates validation to ``StartupContext.validate_populated()`` which
        raises ``RuntimeError`` listing any missing components so that startup
        failures are diagnosed immediately.
        """
        populated = ctx.validate_populated()
        return AppComponents(
            shutdown_mgr=populated.shutdown_mgr,
            components=populated.components,
            scheduler=populated.scheduler,
            channel=populated.channel,
            pipeline=populated.pipeline,
            executor=populated.executor,
            workspace_monitor=populated.workspace_monitor,
            config_watcher=populated.config_watcher,
        )

    @staticmethod
    def _reconfigure_logging(config: Config) -> None:
        """Reconfigure logging after a config change (e.g. verbosity)."""
        import logging

        from src.logging.logging_config import VerbosityLevel, setup_logging

        verbosity = VerbosityLevel(config.log_verbosity.lower())
        level = {
            VerbosityLevel.QUIET: logging.WARNING,
            VerbosityLevel.NORMAL: logging.INFO,
            VerbosityLevel.VERBOSE: logging.DEBUG,
        }[verbosity]

        setup_logging(
            level=level,
            log_format=config.log_format,
            include_correlation_id=True,
            suppress_noisy=True,
            log_file=config.log_file,
            log_max_bytes=config.log_max_bytes,
            log_backup_count=config.log_backup_count,
            verbosity=verbosity,
        )

    def _swap_config(self, new_config: Config) -> None:
        """Atomically replace the application-level config reference.

        Called by :class:`ConfigChangeApplier` during hot-reload to ensure
        ``Application._config`` is updated in sync with the applier's internal
        reference.  A single attribute assignment guarantees no coroutine
        observes a partially-updated config.
        """
        self._config = new_config

    # ── Pipeline Construction ───────────────────────────────────────────

    def _build_pipeline(
        self,
        *,
        shutdown_mgr: GracefulShutdown,
        components: BotComponents,
        channel: BaseChannel,
    ) -> MessagePipeline:
        """Build the message-processing middleware chain from config.

        Takes explicit component parameters so it can be called during
        the ``STARTING`` phase before ``self._state`` is populated.

        Uses ``build_pipeline_from_config()`` so the middleware order and
        custom middleware paths are driven by ``config.json``.  Falls back
        to the built-in default order when ``middleware_order`` is empty.
        """
        from src.core.message_pipeline import PipelineDependencies, build_pipeline_from_config

        mw_cfg = self._config.middleware
        deps = PipelineDependencies(
            shutdown_mgr=shutdown_mgr,
            session_metrics=self._session_metrics,
            bot=components.bot,
            channel=channel,
            verbose=self._verbose,
            dedup=components.dedup,
        )
        return build_pipeline_from_config(
            middleware_order=mw_cfg.middleware_order,
            extra_middleware_paths=mw_cfg.extra_middleware_paths,
            deps=deps,
        )

    # ── Wiring ──────────────────────────────────────────────────────────

    @staticmethod
    def _wire_scheduler(
        *,
        channel: BaseChannel,
        bot: Bot,
        scheduler: TaskScheduler,
    ) -> None:
        """Wire scheduler callbacks to the WhatsApp channel.

        Takes explicit parameters so it can be called during the
        ``STARTING`` phase before ``self._state`` is populated.
        """
        # skip_delays=True bypasses human-like stealth delays for scheduled messages
        scheduler.set_on_send(
            lambda chat_id, text: channel.send_message(chat_id, text, skip_delays=True)
        )

        scheduler.set_on_trigger(
            lambda chat_id, prompt, prompt_hmac: bot.process_scheduled(
                chat_id, prompt, channel=channel, prompt_hmac=prompt_hmac
            )
        )

    # ── Message Handler ─────────────────────────────────────────────────

    async def _on_message(self, msg: IncomingMessage) -> None:
        """Handle incoming message via the middleware pipeline."""
        if not self.state.shutdown_mgr.accepting_messages:
            log.debug("Rejecting message from %s - shutdown in progress", msg.chat_id)
            return

        async with self._message_semaphore:
            # Re-check after acquiring — shutdown may have started while queued.
            if not self.state.shutdown_mgr.accepting_messages:
                log.debug("Rejecting message from %s - shutdown while queued", msg.chat_id)
                return

            # Propagate correlation ID from the incoming message (or generate a
            # fresh one) so that all downstream logging and event emission can be
            # traced back to this message.
            set_correlation_id(msg.correlation_id)

            ctx = MessageContext(msg=msg)
            try:
                await self.state.pipeline.execute(ctx)
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
                    log_noncritical(
                        NonCriticalCategory.EVENT_EMISSION,
                        "Failed to emit error_occurred event for chat %s",
                        msg.chat_id,
                        logger=log,
                    )
                raise
            finally:
                clear_correlation_id()

    # ── Shutdown Phase ──────────────────────────────────────────────────

    async def _shutdown_cleanup(self) -> None:
        """Delegate to the shared ordered-shutdown sequence."""
        if self._phase == AppPhase.RUNNING:
            self._transition(AppPhase.SHUTTING_DOWN)

        state = self._state
        if state is None:
            # Startup never completed — nothing to clean up.
            return

        # Stop config watcher before general shutdown
        try:
            await asyncio.wait_for(
                state.config_watcher.stop(), timeout=CLEANUP_STEP_TIMEOUT
            )
        except asyncio.TimeoutError:
            log_noncritical(
                NonCriticalCategory.SHUTDOWN,
                "Config watcher stop timed out after %.1fs",
                CLEANUP_STEP_TIMEOUT,
                logger=log,
                level=logging.WARNING,
                exc_info=False,
                extra={
                    "shutdown_step": "config_watcher_stop",
                    "timeout_seconds": CLEANUP_STEP_TIMEOUT,
                    "affected_components": ["config_watcher"],
                },
            )
        except Exception as exc:
            log.warning("Error stopping config watcher: %s", exc)

        # Stop workspace monitor before general shutdown
        try:
            await asyncio.wait_for(
                state.workspace_monitor.stop(), timeout=CLEANUP_STEP_TIMEOUT
            )
        except asyncio.TimeoutError:
            log_noncritical(
                NonCriticalCategory.SHUTDOWN,
                "Workspace monitor stop timed out after %.1fs",
                CLEANUP_STEP_TIMEOUT,
                logger=log,
                level=logging.WARNING,
                exc_info=False,
                extra={
                    "shutdown_step": "workspace_monitor_stop",
                    "timeout_seconds": CLEANUP_STEP_TIMEOUT,
                    "affected_components": ["workspace_monitor"],
                },
            )
        except Exception as exc:
            log.warning("Error stopping workspace monitor: %s", exc)

        try:
            await asyncio.wait_for(
                perform_shutdown(ShutdownContext(
                    shutdown=state.shutdown_mgr,
                    channel=state.channel,
                    scheduler=state.scheduler,
                    health_server=self._health_server,
                    db=state.components.db,
                    vector_memory=state.components.vector_memory,
                    project_store=state.components.project_store,
                    message_queue=state.components.message_queue,
                    llm=state.components.llm,
                    session_metrics=self._session_metrics.to_dict(),
                    log=log,
                    verbose=self._verbose,
                    bot=state.components.bot,
                    executor=state.executor,
                    routing_engine=state.components.routing_engine,
                )),
                timeout=CLEANUP_STEP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log_noncritical(
                NonCriticalCategory.SHUTDOWN,
                "Perform shutdown timed out after %.1fs",
                CLEANUP_STEP_TIMEOUT,
                logger=log,
                level=logging.WARNING,
                exc_info=False,
                extra={
                    "shutdown_step": "perform_shutdown",
                    "timeout_seconds": CLEANUP_STEP_TIMEOUT,
                    "affected_components": [
                        "channel", "scheduler", "health_server", "db",
                        "vector_memory", "project_store", "message_queue",
                        "llm", "bot", "executor", "routing_engine",
                    ],
                },
            )

        self._transition(AppPhase.STOPPED)
