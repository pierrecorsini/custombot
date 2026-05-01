"""
src/core/startup.py — Declarative component registry for application startup.

Replaces the monolithic ``Application._startup()`` with a data-driven
approach: each component is described by a ``ComponentSpec`` (name,
factory callable, optional detail string) and the ``StartupOrchestrator``
executes them in order, handling logging, timing, and error propagation.

The architecture mirrors ``message_pipeline.py``'s declarative factory
registry (``BUILTIN_MIDDLEWARE_FACTORIES`` + ``DEFAULT_MIDDLEWARE_ORDER``).

Usage::

    from src.core.startup import StartupOrchestrator, StartupContext

    ctx = StartupContext(config=config, session_metrics=metrics, app=app)
    orchestrator = StartupOrchestrator(ctx)
    startup_time = await orchestrator.run_all()
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Protocol, Sequence

from src.config import CONFIG_PATH
from src.constants import (
    CONFIG_WATCH_INTERVAL_SECONDS,
    DEFAULT_SHUTDOWN_TIMEOUT,
    DEFAULT_THREAD_POOL_WORKERS,
    WORKSPACE_CLEANUP_INTERVAL_SECONDS,
    WORKSPACE_DIR,
)
from src.core.orchestrator import StepOrchestrator
from src.lifecycle import _log_startup_begin

if TYPE_CHECKING:
    from src.app import Application
    from src.builder import BotComponents
    from src.channels.base import BaseChannel
    from src.config import Config
    from src.config.config_watcher import ConfigWatcher
    from src.core.message_pipeline import MessagePipeline
    from src.health import HealthServer
    from src.monitoring import SessionMetrics
    from src.monitoring.workspace_monitor import WorkspaceMonitor
    from src.scheduler import TaskScheduler
    from src.shutdown import GracefulShutdown

log = logging.getLogger(__name__)


# ── Infrastructure ───────────────────────────────────────────────────────


@dataclass(slots=True)
class StartupContext:
    """Mutable state bag shared across all startup steps.

    Each step reads from and writes to this object.  Fields start as
    ``None`` and are populated as steps execute.
    """

    config: Config
    session_metrics: SessionMetrics
    app: Application  # TYPE_CHECKING-guarded to avoid circular import

    # Populated by steps
    shutdown_mgr: GracefulShutdown | None = None
    executor: ThreadPoolExecutor | None = None
    components: BotComponents | None = None
    scheduler: TaskScheduler | None = None
    channel: BaseChannel | None = None
    pipeline: MessagePipeline | None = None
    workspace_monitor: WorkspaceMonitor | None = None
    config_watcher: ConfigWatcher | None = None
    health_server: HealthServer | None = None

    # Tracking
    initialized_components: list[str] = field(default_factory=list)
    component_durations: dict[str, float] = field(default_factory=dict)


class StartupStepFactory(Protocol):
    """Protocol for a factory that initialises one startup component."""

    async def __call__(self, ctx: StartupContext) -> str | None:
        """Execute the startup step.

        Returns an optional detail string for the "READY" log line
        (e.g. ``"max_workers=4"``).  Return ``None`` for no detail.
        """
        ...


@dataclass(slots=True, frozen=True)
class ComponentSpec:
    """Declarative description of a single startup step.

    Attributes:
        name: Human-readable name used in log lines and tracking.
        factory: Async callable that receives ``StartupContext`` and returns
                 an optional detail string for the ready-log.
        label: Optional tracking label appended to ``initialized_components``.
               Defaults to *name* if not provided.  Use ``"__none__"`` to
               suppress tracking entirely (for steps that are pure wiring).
        depends_on: Names of steps that must complete before this step runs.
                    The orchestrator resolves execution order via topological
                    sort.  Empty (default) means no prerequisites.
    """

    name: str
    factory: StartupStepFactory
    label: str | None = None
    depends_on: Sequence[str] = ()


# ── Helper classes ─────────────────────────────────────────────────────


class _NoOpApplier:
    """Fallback config-change applier used when the channel doesn't support hot-reload."""

    def apply(self, old_config, new_config):
        log.debug("Config change detected but channel is mocked — skipping apply")


# ── Step implementations ────────────────────────────────────────────────


async def _step_shutdown_manager(ctx: StartupContext) -> str | None:
    """Create and register the graceful-shutdown manager."""
    from src.shutdown import GracefulShutdown

    timeout = (
        ctx.config.shutdown_timeout
        if ctx.config.shutdown_timeout is not None
        else DEFAULT_SHUTDOWN_TIMEOUT
    )
    mgr = GracefulShutdown(timeout=timeout)
    loop = asyncio.get_running_loop()
    mgr.register_signal_handlers(loop)
    ctx.shutdown_mgr = mgr
    ctx.app._shutdown_mgr = mgr
    return None


async def _step_thread_pool(ctx: StartupContext) -> str | None:
    """Create and install the default thread-pool executor."""
    workers = ctx.config.max_thread_pool_workers or DEFAULT_THREAD_POOL_WORKERS
    executor = ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="cb-worker"
    )
    asyncio.get_running_loop().set_default_executor(executor)
    ctx.executor = executor
    ctx.app._executor = executor
    return f"max_workers={workers}"


async def _step_bot_components(ctx: StartupContext) -> str | None:
    """Build and validate all bot sub-components."""
    from src.builder import _build_bot

    components = await _build_bot(ctx.config, session_metrics=ctx.session_metrics)
    components.bot.validate_wiring()
    ctx.components = components
    ctx.app._components = components

    # Pre-warm the httpx connection pool so the first user message
    # doesn't pay the TCP + TLS handshake latency.
    await components.llm.warmup()

    # Pre-warm VectorMemory read connection so the first semantic search
    # avoids the sqlite-vec extension loading latency (~5ms).
    if components.vector_memory is not None:
        await asyncio.to_thread(components.vector_memory.warmup)

    # Track multiple sub-components
    ctx.initialized_components.append("Bot (LLM, Memory, Skills, Routing)")
    ctx.initialized_components.append("Database")
    return "all subsystems ready"


async def _step_scheduler(ctx: StartupContext) -> str | None:
    """Create, configure, and start the task scheduler."""
    from pathlib import Path

    from src.scheduler import TaskScheduler
    from src.skills.builtin.task_scheduler import set_scheduler_instance

    workspace = Path(WORKSPACE_DIR)
    scheduler = TaskScheduler()
    scheduler.set_dedup_service(ctx.components.dedup)
    # on_trigger/on_send are wired later by _step_wire_scheduler, which has
    # access to the channel needed for callback routing.
    scheduler.configure(workspace=workspace)
    set_scheduler_instance(scheduler)
    await scheduler.load_all()
    scheduler.start()
    ctx.scheduler = scheduler
    ctx.app._scheduler = scheduler
    return f"workspace={workspace}"


async def _step_channel(ctx: StartupContext) -> str | None:
    """Create the WhatsApp channel."""
    from src.channels.whatsapp import WhatsAppChannel

    channel = WhatsAppChannel(
        ctx.config.whatsapp,
        safe_mode=ctx.app._safe_mode,
        load_history=ctx.config.load_history,
    )
    ctx.channel = channel
    ctx.app._channel = channel
    return f"provider={ctx.config.whatsapp.provider}"


async def _step_wire_scheduler(ctx: StartupContext) -> str | None:
    """Wire scheduler callbacks to the channel."""
    ctx.app._wire_scheduler(
        channel=ctx.channel,
        bot=ctx.components.bot,
        scheduler=ctx.scheduler,
    )
    return None


async def _step_health_server(ctx: StartupContext) -> str | None:
    """Start health check server if a port was configured."""
    health_port = ctx.app._health_port
    if not health_port:
        return None

    from src.health import HealthServer

    # Merge sub-component durations from the builder into the
    # orchestration-level durations so the /health endpoint has a
    # complete picture of every init phase.
    startup_durations = dict(ctx.component_durations)
    if ctx.components is not None and ctx.components.component_durations:
        startup_durations.update(ctx.components.component_durations)

    try:
        health_server = HealthServer(
            db=ctx.components.db,
            token_usage=ctx.components.token_usage,
            bot=ctx.components.bot,
            scheduler=ctx.scheduler,
            llm_log_dir=(
                f"{WORKSPACE_DIR}/logs/llm" if ctx.config.log_llm else None
            ),
            workspace_dir=WORKSPACE_DIR,
            shutdown_mgr=ctx.shutdown_mgr,
            startup_durations=startup_durations,
            vector_memory=ctx.components.vector_memory,
        )
        await health_server.start(port=health_port)
        ctx.health_server = health_server
        ctx.app._health_server = health_server
        ctx.initialized_components.append(f"Health Server (port {health_port})")
        return f"port={health_port}"
    except Exception as exc:
        log.warning("Failed to start health server on port %d: %s", health_port, exc)
        ctx.app._health_server = None
        return None


async def _step_recover_messages(ctx: StartupContext) -> str | None:
    """Recover stale messages from previous crash/restart."""
    await ctx.components.bot.recover_pending_messages(channel=ctx.channel)
    return None


async def _step_pipeline(ctx: StartupContext) -> str | None:
    """Build the message-processing middleware chain from config."""
    pipeline = ctx.app._build_pipeline(
        shutdown_mgr=ctx.shutdown_mgr,
        components=ctx.components,
        channel=ctx.channel,
    )
    ctx.pipeline = pipeline
    ctx.app._pipeline = pipeline
    return None


async def _step_workspace_monitor(ctx: StartupContext) -> str | None:
    """Start workspace size monitoring and periodic cleanup."""
    from src.monitoring.workspace_monitor import WorkspaceMonitor

    monitor = WorkspaceMonitor(workspace_dir=WORKSPACE_DIR)
    monitor.start_periodic_cleanup()
    ctx.workspace_monitor = monitor
    ctx.app._workspace_monitor = monitor
    return f"interval={WORKSPACE_CLEANUP_INTERVAL_SECONDS:.0f}s"


async def _step_config_watcher(ctx: StartupContext) -> str | None:
    """Start config hot-reload watcher (polling-based)."""
    from src.config.config_watcher import ConfigWatcher

    channel = ctx.channel

    applier: Any = channel.create_config_applier(
        app_config=ctx.config,
        bot=ctx.components.bot,
        llm=ctx.components.llm,
        shutdown_mgr=ctx.shutdown_mgr,
        reconfigure_logging=ctx.app._reconfigure_logging,
    )
    if applier is None:
        log.debug(
            "Config watcher: channel does not support hot-reload — using no-op applier"
        )

        applier = _NoOpApplier()

    watcher = ConfigWatcher(
        config_path=CONFIG_PATH,
        current_config=ctx.config,
        applier=applier,
    )
    watcher.start()
    ctx.config_watcher = watcher
    ctx.app._config_watcher = watcher
    return f"path={CONFIG_PATH}, interval={CONFIG_WATCH_INTERVAL_SECONDS:.0f}s"


# ── Default step registry ───────────────────────────────────────────────


DEFAULT_STARTUP_STEPS: list[ComponentSpec] = [
    ComponentSpec(name="Shutdown Manager", factory=_step_shutdown_manager),
    ComponentSpec(
        name="Thread Pool",
        factory=_step_thread_pool,
        label="Thread Pool ({workers} workers)",
    ),
    ComponentSpec(
        name="Bot Components",
        factory=_step_bot_components,
        label="__none__",  # step itself appends to initialized_components
    ),
    ComponentSpec(
        name="Task Scheduler",
        factory=_step_scheduler,
        depends_on=("Bot Components",),
    ),
    ComponentSpec(name="WhatsApp Channel", factory=_step_channel),
    ComponentSpec(
        name="Wire Scheduler",
        factory=_step_wire_scheduler,
        label="__none__",
        depends_on=("Task Scheduler", "WhatsApp Channel"),
    ),
    ComponentSpec(
        name="Health Server",
        factory=_step_health_server,
        label="__none__",  # step itself appends when port is configured
        depends_on=("Bot Components", "Task Scheduler"),
    ),
    ComponentSpec(
        name="Recover Pending Messages",
        factory=_step_recover_messages,
        label="__none__",
        depends_on=("Bot Components", "WhatsApp Channel"),
    ),
    ComponentSpec(
        name="Pipeline",
        factory=_step_pipeline,
        label="__none__",
        depends_on=("Bot Components", "WhatsApp Channel"),
    ),
    ComponentSpec(name="Workspace Monitor", factory=_step_workspace_monitor),
    ComponentSpec(
        name="Config Watcher",
        factory=_step_config_watcher,
        depends_on=("WhatsApp Channel", "Bot Components"),
    ),
]


# ── Orchestrator ────────────────────────────────────────────────────────


class StartupOrchestrator(StepOrchestrator[StartupContext, ComponentSpec]):
    """Execute a sequence of ``ComponentSpec`` steps in order.

    Handles logging, timing, and error propagation for each step.
    The pattern mirrors ``message_pipeline.py``'s declarative approach.
    """

    __slots__ = ()

    def __init__(
        self,
        ctx: StartupContext,
        steps: Sequence[ComponentSpec] | None = None,
    ) -> None:
        super().__init__(
            ctx,
            steps,
            DEFAULT_STARTUP_STEPS,
            context_label="startup dependency",
        )

    async def run_all(self) -> float:
        """Run all startup steps and return the ``_log_startup_begin`` timestamp.

        Steps are executed in dependency-resolved order.  On failure, the
        exception propagates and the caller is responsible for cleaning up
        any partially-initialised components.
        """
        startup_time = _log_startup_begin(self._ctx.config)

        for spec in self._resolve_order():
            await self._execute_step(spec)

            # Track in the initialized-components list unless suppressed
            label = spec.label if spec.label is not None else spec.name
            if label != "__none__":
                self._ctx.initialized_components.append(label)

        return startup_time
