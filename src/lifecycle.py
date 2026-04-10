"""
src/lifecycle.py — Startup and shutdown lifecycle logging helpers.

Structured logging for bot startup/shutdown phases with timing,
component status, and session metrics.
"""

from __future__ import annotations

import logging
import time

from src.config import Config
from src.constants import DEFAULT_SHUTDOWN_TIMEOUT, WORKSPACE_DIR


log = logging.getLogger("lifecycle")


def _get_verbosity() -> str:
    """Get current verbosity level from logging config."""
    try:
        from src.logging.logging_config import get_verbosity

        return get_verbosity().value
    except Exception:
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
            "llm_base_url": config.llm.base_url,
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


def _log_connection_status(
    service: str, status: str, details: str | None = None
) -> None:
    """Log connection status for external services."""
    verbosity = _get_verbosity()
    if verbosity == "quiet":
        return

    if verbosity == "verbose":
        if details:
            log.info("[CONNECTION] %s - %s (%s)", service.upper(), status, details)
        else:
            log.info("[CONNECTION] %s - %s", service.upper(), status)
    # Normal mode: skip connection logs (handled by CLI output)


def _log_startup_complete(start_time: float, components: list[str]) -> None:
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
        log.info("")
    else:
        # Normal mode: single line summary
        log.info(
            "Startup complete (%.2fs) — %d components ready", duration, len(components)
        )


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
        log.info(
            "  %-25s = %d", "messages_processed", metrics.get("messages_processed", 0)
        )
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
