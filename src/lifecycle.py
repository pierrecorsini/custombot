"""
src/lifecycle.py — Startup and shutdown lifecycle logging helpers.

Structured logging for bot startup/shutdown phases with timing,
component status, and session metrics.
"""

from __future__ import annotations

import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from src.config import Config
from src.constants import CLEANUP_STEP_TIMEOUT
from src.core.errors import NonCriticalCategory, log_noncritical
from src.security.url_sanitizer import sanitize_url_for_logging

if TYPE_CHECKING:
    from src.bot import Bot
    from src.channels.base import BaseChannel
    from src.db import Database
    from src.health import HealthServer
    from src.llm_provider import LLMProvider
    from src.message_queue import MessageQueue
    from src.project.store import ProjectStore
    from src.scheduler import TaskScheduler
    from src.shutdown import GracefulShutdown
    from src.vector_memory import VectorMemory
    from src.constants import DEFAULT_SHUTDOWN_TIMEOUT, WORKSPACE_DIR

log = logging.getLogger("lifecycle")


def _get_verbosity() -> str:
    """Get current verbosity level from logging config."""
    try:
        from src.logging.logging_config import get_verbosity

        return get_verbosity().value
    except Exception:
        log_noncritical(
            NonCriticalCategory.CONFIG_LOAD,
            "Failed to resolve verbosity level, defaulting to 'normal'",
            logger=log,
        )
        return "normal"


def _log_startup_begin(config: Config) -> float:
    """
    Log startup begin with configuration summary (redacted).

    Returns the start time for duration tracking.
    """
    start_time = time.time()
    verbosity = _get_verbosity()

    if verbosity == "quiet":
        # Quiet mode: minimal startup message
        log.info("Starting up...")
        return start_time

    if verbosity == "verbose":
        # Verbose mode: full config summary
        config_summary = {
            "llm_model": config.llm.model,
            "llm_base_url": sanitize_url_for_logging(config.llm.base_url),
            "llm_api_key": "***REDACTED***" if config.llm.api_key else "NOT_SET",
            "whatsapp_provider": config.whatsapp.provider,
            "neonize_db_path": config.whatsapp.neonize.db_path,
            "workspace": WORKSPACE_DIR,
            "max_history": config.memory_max_history,
            "skills_auto_load": config.skills_auto_load,
            "skills_user_directory": config.skills_user_directory,
            "log_format": config.log_format,
            "shutdown_timeout": config.shutdown_timeout or DEFAULT_SHUTDOWN_TIMEOUT,
        }

        log.info("=" * 60)
        log.info("STARTUP BEGIN")
        log.info("=" * 60)
        log.info("Configuration summary (redacted):")
        for key, value in config_summary.items():
            log.info("  %-25s = %s", key, value)
    else:
        # Normal mode: single line summary
        log.info(
            "Startup: model=%s, provider=%s",
            config.llm.model,
            config.whatsapp.provider,
        )

    return start_time


def _log_component_init(component_name: str, status: str = "started") -> None:
    """Log component initialization status."""
    verbosity = _get_verbosity()
    if verbosity == "quiet":
        return

    if verbosity == "verbose":
        log.info("[COMPONENT] %s - %s", component_name.upper(), status)
    # Normal mode: skip individual component init logs


def _log_component_ready(component_name: str, details: str | None = None) -> None:
    """Log component ready status with optional details."""
    verbosity = _get_verbosity()
    if verbosity == "quiet":
        return

    if verbosity == "verbose":
        if details:
            log.info("[COMPONENT] %s - READY (%s)", component_name.upper(), details)
        else:
            log.info("[COMPONENT] %s - READY", component_name.upper())
    # Normal mode: skip individual component ready logs


def _log_skills_loaded(skills_registry) -> None:
    """Log skill loading status."""
    verbosity = _get_verbosity()
    if verbosity == "quiet":
        return

    skills_list = skills_registry.all()

    if verbosity == "verbose":
        log.info("[SKILLS] Loaded %d skill(s):", len(skills_list))
        for skill in skills_list:
            log.info("  - %s: %s", skill.name, skill.description[:60])
    else:
        # Normal mode: single line
        log.info("Skills loaded: %d", len(skills_list))


def _log_startup_complete(
    start_time: float,
    components: list[str],
    component_durations: dict[str, float] | None = None,
) -> None:
    """Log startup completion with timing and component summary."""
    duration = time.time() - start_time
    verbosity = _get_verbosity()

    if verbosity == "quiet":
        return

    if verbosity == "verbose":
        log.info("=" * 60)
        log.info("STARTUP COMPLETE")
        log.info("=" * 60)
        log.info("Startup duration: %.2fs", duration)
        log.info("Components initialized (%d):", len(components))
        for comp in components:
            log.info("  ✓ %s", comp)
        if component_durations:
            log.info("Per-component init timing:")
            for name, dur in component_durations.items():
                log.info("  %-25s %.3fs", name, dur)
        log.info("")
    else:
        # Normal mode: single line summary
        log.info("Startup complete (%.2fs) — %d components ready", duration, len(components))


def _log_shutdown_begin(metrics: dict) -> None:
    """Log shutdown begin with session metrics."""
    verbosity = _get_verbosity()

    if verbosity == "quiet":
        return

    if verbosity == "verbose":
        log.info("")
        log.info("=" * 60)
        log.info("SHUTDOWN BEGIN")
        log.info("=" * 60)
        log.info("Session metrics:")
        log.info("  %-25s = %s", "uptime", f"{metrics.get('uptime', 0):.1f}s")
        log.info("  %-25s = %d", "messages_processed", metrics.get("messages_processed", 0))
        log.info("  %-25s = %d", "skills_executed", metrics.get("skills_executed", 0))
        log.info("  %-25s = %d", "errors_count", metrics.get("errors_count", 0))
    else:
        # Normal mode: single line
        log.info(
            "Shutdown: uptime=%.1fs, messages=%d, errors=%d",
            metrics.get("uptime", 0),
            metrics.get("messages_processed", 0),
            metrics.get("errors_count", 0),
        )


def _log_cleanup_step(step_num: int, total_steps: int, description: str) -> None:
    """Log cleanup step progress."""
    verbosity = _get_verbosity()
    if verbosity == "quiet":
        return

    if verbosity == "verbose":
        log.info("[CLEANUP %d/%d] %s", step_num, total_steps, description)
    # Normal mode: skip individual cleanup step logs


def _log_shutdown_complete(start_time: float) -> None:
    """Log shutdown completion with timing."""
    verbosity = _get_verbosity()

    if verbosity == "quiet":
        return

    duration = time.time() - start_time

    if verbosity == "verbose":
        log.info("=" * 60)
        log.info("SHUTDOWN COMPLETE")
        log.info("=" * 60)
        log.info("Shutdown duration: %.2fs", duration)
        log.info("")
    else:
        # Normal mode: single line
        log.info("Shutdown complete (%.2fs)", duration)


@dataclass(slots=True)
class ShutdownContext:
    """Structured parameter bag for ``perform_shutdown()``.

    Replaces the former 14-parameter signature with a single dataclass,
    mirroring ``StartupContext`` from ``src/core/startup.py``.

    Required fields correspond to components that are always available
    at shutdown time.  Optional fields (``health_server``, ``bot``,
    ``executor``) may be ``None`` depending on configuration and startup
    success.
    """

    shutdown: GracefulShutdown
    channel: BaseChannel
    scheduler: TaskScheduler
    db: Database
    project_store: ProjectStore
    message_queue: MessageQueue
    llm: LLMProvider
    session_metrics: dict
    log: logging.Logger

    # Optional components
    health_server: HealthServer | None = None
    vector_memory: VectorMemory | None = None
    bot: Bot | None = None
    executor: ThreadPoolExecutor | None = None
    verbose: bool = False


async def perform_shutdown(ctx: ShutdownContext) -> None:
    """Execute the 7-step graceful shutdown sequence."""
    from src.ui.cli_output import cli as cli_output

    shutdown_begin_time = time.time()
    if "uptime" not in ctx.session_metrics:
        ctx.session_metrics["uptime"] = time.time() - ctx.session_metrics.get("start_time", time.time())

    _log_shutdown_begin(ctx.session_metrics)
    cli_output.warning("Initiating graceful shutdown...")

    total_cleanup_steps = 7

    # 1. Stop accepting new messages
    _log_cleanup_step(1, total_cleanup_steps, "Stopping message acceptance and polling")
    ctx.shutdown.request_shutdown()
    ctx.channel.request_shutdown()

    # 2. Wait for in-flight operations
    _log_cleanup_step(2, total_cleanup_steps, "Waiting for in-flight operations")
    cli_output.dim("  Waiting for in-flight operations to complete...")
    completed = await ctx.shutdown.wait_for_in_flight()
    if not completed:
        ctx.log.warning("Force proceeding after timeout")
        cli_output.warning("Timed out waiting for operations, forcing shutdown")

    # 3. Stop scheduler and health server in parallel
    _log_cleanup_step(3, total_cleanup_steps, "Stopping scheduler, health server, and channel")
    cli_output.dim("  Stopping background services...")

    async def _stop_scheduler():
        try:
            await ctx.scheduler.stop()
        except Exception as exc:
            ctx.log.warning("Error stopping scheduler: %s", exc)

    async def _stop_health():
        if ctx.health_server:
            try:
                await ctx.health_server.stop()
            except Exception as exc:
                ctx.log.warning("Error stopping health server: %s", exc)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(_stop_scheduler())
        tg.create_task(_stop_health())

    # 4. Close channel
    _log_cleanup_step(4, total_cleanup_steps, "Closing channel connections")
    cli_output.dim("  Closing channel connections...")
    try:
        await ctx.channel.close()
    except Exception as exc:
        ctx.log.warning("Error closing channel: %s", exc)

    # 5. Close project store, vector memory, message queue, and LLM client in parallel
    _log_cleanup_step(5, total_cleanup_steps, "Closing project store, vector memory, message queue, and LLM")
    cli_output.dim("  Closing storage backends and LLM client...")

    def _close_project_store():
        try:
            ctx.project_store.close()
        except Exception as exc:
            ctx.log.warning("Error closing project store: %s", exc)

    def _close_vector_memory():
        if ctx.vector_memory is None:
            return
        try:
            ctx.vector_memory.close()
        except Exception as exc:
            ctx.log.warning("Error closing vector memory: %s", exc)

    async def _close_message_queue():
        try:
            await ctx.message_queue.close()
        except Exception as exc:
            ctx.log.warning("Error closing message queue: %s", exc)

    async def _close_llm():
        try:
            await ctx.llm.close()
        except Exception as exc:
            ctx.log.warning("Error closing LLM client: %s", exc)

    async def _stop_memory_monitoring():
        if ctx.bot is None:
            return
        try:
            await ctx.bot.stop_memory_monitoring()
        except Exception as exc:
            ctx.log.warning("Error stopping memory monitoring: %s", exc)

    def _close_executor():
        if ctx.bot is None:
            return
        try:
            ctx.bot.close_executor()
        except Exception as exc:
            ctx.log.warning("Error closing tool executor: %s", exc)

    await asyncio.gather(
        asyncio.to_thread(_close_project_store),
        asyncio.to_thread(_close_vector_memory),
        _close_message_queue(),
        _close_llm(),
        _stop_memory_monitoring(),
        asyncio.to_thread(_close_executor),
    )

    # 6. Shut down the thread pool executor (after all to_thread calls are done)
    _log_cleanup_step(6, total_cleanup_steps, "Shutting down thread pool executor")
    if ctx.executor is not None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(ctx.executor.shutdown, wait=True),
                timeout=CLEANUP_STEP_TIMEOUT,
            )
        except TimeoutError:
            ctx.log.warning(
                "Thread pool executor shutdown timed out after %.1fs, proceeding",
                CLEANUP_STEP_TIMEOUT,
            )
        except Exception as exc:
            ctx.log.warning("Error shutting down thread pool executor: %s", exc)

    # 7. Close database (must be last — other closers may still write)
    _log_cleanup_step(7, total_cleanup_steps, "Closing database connections")
    cli_output.dim("  Closing database connections...")
    try:
        await ctx.db.close()
    except Exception as exc:
        ctx.log.warning("Error closing database: %s", exc)

    # Safety net: close any leaked SQLite connections via the shared pool.
    # Individual components (VectorMemory, ProjectStore) should have already
    # closed their own connections, but this catches anything that slipped
    # through (e.g. read connections from background threads).
    from src.db.sqlite_utils import SqliteHelper

    leaked = SqliteHelper.close_all_connections()
    if leaked:
        ctx.log.info("SQLite pool safety net closed %d leaked connection(s): %s", len(leaked), ", ".join(leaked))

    _log_shutdown_complete(shutdown_begin_time)
    cli_output.success("Shutdown complete.")
