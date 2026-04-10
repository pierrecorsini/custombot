"""
main.py — CLI entry point for custombot.

Commands:
  start     Start the bot (connects to WhatsApp via neonize)
  options   Open configuration editor (TUI)

Usage:
  python main.py start     # Start the bot
  python main.py options   # Edit configuration
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import Optional

# Fix Windows console encoding for Unicode (emojis, special chars)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click

from src.config import (
    Config,
    LLMConfig,
    load_config,
    save_config,
    CONFIG_PATH,
)
from src.constants import DEFAULT_SHUTDOWN_TIMEOUT
from src.__version__ import __version__
from src.exceptions import format_user_error
from src.channels.base import IncomingMessage
from src.ui.cli_output import cli as cli_output, log_message_flow

# Extracted modules
from src.lifecycle import (
    _log_startup_begin,
    _log_component_init,
    _log_component_ready,
    _log_startup_complete,
    _log_shutdown_begin,
    _log_cleanup_step,
    _log_shutdown_complete,
)
from src.shutdown import GracefulShutdown
from src.builder import _build_bot
from src.scheduler import TaskScheduler
from src.skills.builtin.task_scheduler import set_scheduler_instance


# ─────────────────────────────────────────────────────────────────────────────
# Logging Setup
# ─────────────────────────────────────────────────────────────────────────────


def _setup_logging(
    verbose: bool,
    log_format: str = "text",
    log_file: str | None = None,
    log_max_bytes: int = 10 * 1024 * 1024,
    log_backup_count: int = 5,
    log_verbosity: str = "normal",
) -> None:
    """
    Configure structured logging for the application.

    Args:
        verbose: Legacy flag — treated as shorthand for log_verbosity="verbose".
        log_format: "text" for human-readable, "json" for structured logs.
        log_file: Path to log file for file output (None = no file logging).
        log_max_bytes: Maximum log file size in bytes before rotation.
        log_backup_count: Number of backup log files to keep.
        log_verbosity: Logging verbosity level ("quiet", "normal", "verbose").
    """
    from src.logging.logging_config import setup_logging, VerbosityLevel

    # Resolve verbosity (caller already handles CLI precedence)
    verbosity = VerbosityLevel(log_verbosity.lower())

    # Derive Python log level from verbosity — setup_logging will also adjust
    level = {
        VerbosityLevel.QUIET: logging.WARNING,
        VerbosityLevel.NORMAL: logging.INFO,
        VerbosityLevel.VERBOSE: logging.DEBUG,
    }[verbosity]

    setup_logging(
        level=level,
        log_format=log_format,
        include_correlation_id=True,
        suppress_noisy=True,
        log_file=log_file,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        verbosity=verbosity,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bot Runner
# ─────────────────────────────────────────────────────────────────────────────


async def _run_bot(
    config: Config,
    verbose: bool = False,
    health_port: Optional[int] = None,
    safe_mode: bool = False,
) -> None:
    """Run the bot with all components and graceful shutdown handling."""
    from src.channels.whatsapp import WhatsAppChannel
    from src.health import HealthServer

    # ─── STARTUP PHASE ───────────────────────────────────────────────────────
    startup_time = _log_startup_begin(config)
    initialized_components: list[str] = []

    session_metrics = {
        "start_time": time.time(),
        "messages_processed": 0,
        "skills_executed": 0,
        "errors_count": 0,
    }

    # Initialize graceful shutdown manager
    _log_component_init("Shutdown Manager", "started")
    shutdown = GracefulShutdown(
        timeout=config.shutdown_timeout or DEFAULT_SHUTDOWN_TIMEOUT
    )
    _log_component_ready("Shutdown Manager")
    initialized_components.append("Shutdown Manager")

    # Register signal handlers
    loop = asyncio.get_running_loop()
    shutdown.register_signal_handlers(loop)

    _log_component_init("Bot Components", "started")
    bot, db, vector_memory, project_store = await _build_bot(config)
    _log_component_ready("Bot Components", "all subsystems ready")
    initialized_components.append("Bot (LLM, Memory, Skills, Routing)")
    initialized_components.append("Database")

    # ── Scheduler ─────────────────────────────────────────────────────────
    _log_component_init("Task Scheduler", "started")
    from src.constants import WORKSPACE_DIR

    scheduler = TaskScheduler()
    workspace = Path(WORKSPACE_DIR)
    scheduler.configure(
        workspace=workspace,
        on_trigger=lambda chat_id, prompt: bot.process_scheduled(chat_id, prompt),
    )
    set_scheduler_instance(scheduler)
    scheduler.load_all()
    scheduler.start()
    _log_component_ready("Task Scheduler", f"workspace={workspace}")
    initialized_components.append("Task Scheduler")

    _log_component_init("WhatsApp Channel", "started")
    channel = WhatsAppChannel(
        config.whatsapp, safe_mode=safe_mode, load_history=config.load_history
    )
    _log_component_ready("WhatsApp Channel", f"provider={config.whatsapp.provider}")
    initialized_components.append("WhatsApp Channel")

    # Wire scheduler's on_send to the WhatsApp channel so results get delivered
    # skip_delays=True bypasses human-like stealth delays for scheduled messages
    scheduler._on_send = lambda chat_id, text: channel.send_message(
        chat_id, text, skip_delays=True
    )

    # Update on_trigger to include the channel for prompt injection
    scheduler._on_trigger = lambda chat_id, prompt: bot.process_scheduled(
        chat_id,
        prompt,
        channel=channel,
    )

    # Start health check server if port is specified
    health_server: Optional[HealthServer] = None
    if health_port:
        _log_component_init("Health Server", "started")
        try:
            health_server = HealthServer(
                db=db,
                check_bridge=False,
            )
            await health_server.start(port=health_port)
            _log_component_ready("Health Server", f"port={health_port}")
            initialized_components.append(f"Health Server (port {health_port})")
            cli_output.dim(
                f"  Health check endpoint: http://0.0.0.0:{health_port}/health"
            )
        except Exception as e:
            logging.getLogger(__name__).warning(
                "Failed to start health server on port %d: %s", health_port, e
            )
            health_server = None

    async def on_message(msg: IncomingMessage) -> None:
        """Handle incoming message with graceful shutdown awareness."""
        if not shutdown.accepting_messages:
            logging.getLogger(__name__).debug(
                "Rejecting message from %s - shutdown in progress", msg.chat_id
            )
            return

        op_id = await shutdown.enter_operation(
            f"message from {msg.sender_name or msg.sender_id} in {msg.chat_id}"
        )
        if op_id is None:
            return

        # Create streaming callback with captured chat_id for real-time tool updates
        async def stream_tool_update(text: str) -> None:
            """Send tool execution updates to WhatsApp as they happen."""
            try:
                await channel.send_message(msg.chat_id, text)
                # Small delay to avoid WhatsApp rate limiting (max ~1 msg/sec)
                await asyncio.sleep(0.5)
            except Exception as exc:
                # Log but don't fail the main flow if streaming fails
                logging.getLogger(__name__).warning(
                    "Failed to stream tool update to %s: %s", msg.chat_id, exc
                )

        try:
            session_metrics["messages_processed"] += 1
            # Log incoming message with structured format
            log_message_flow(
                direction="IN",
                channel=msg.channel_type or "unknown",
                source=msg.sender_name or msg.sender_id,
                destination=msg.chat_id,
                text=msg.text,
                from_me=msg.fromMe,
                to_me=msg.toMe,
            )
            # Pre-check filters before showing typing indicator
            # Avoids revealing bot activity for messages that will be filtered
            preflight = await bot.preflight_check(msg)
            if not preflight:
                return
            # Show "typing..." indicator only for messages that pass filters
            await channel.send_typing(msg.chat_id)
            try:
                # Pass stream_callback to enable real-time tool updates
                response = await bot.handle_message(
                    msg, channel=channel, stream_callback=stream_tool_update
                )
                if response:
                    log_message_flow(
                        direction="OUT",
                        channel=msg.channel_type or "unknown",
                        source="Bot",
                        destination=msg.chat_id,
                        text=response,
                        from_me=True,
                        to_me=False,
                    )
                    await channel.send_message(msg.chat_id, response)
            except Exception as exc:
                session_metrics["errors_count"] += 1
                from src.logging import get_correlation_id

                corr_id = get_correlation_id()
                error_msg = format_user_error(exc, correlation_id=corr_id)
                logging.getLogger(__name__).error(
                    "Error handling message: %s", exc, exc_info=verbose
                )
                await channel.send_message(msg.chat_id, error_msg)
        finally:
            await shutdown.exit_operation(op_id)

    log = logging.getLogger(__name__)

    # Create polling task — channel.start connects to WhatsApp via neonize
    _log_component_init("Message Poller", "started")
    poll_task = asyncio.create_task(channel.start(on_message))
    _log_component_ready("Message Poller")
    initialized_components.append("Message Poller")

    _log_startup_complete(startup_time, initialized_components)

    try:
        cli_output.info("Listening...  (Ctrl+C to stop)")
        await shutdown.wait_for_shutdown()

    except Exception as e:
        log.error("Unexpected error in main loop: %s", e, exc_info=verbose)
        session_metrics["errors_count"] += 1

    finally:
        # ─── SHUTDOWN PHASE ───────────────────────────────────────────────────
        shutdown_begin_time = time.time()
        session_metrics["uptime"] = time.time() - session_metrics["start_time"]

        _log_shutdown_begin(session_metrics)
        cli_output.warning("Initiating graceful shutdown...")

        total_cleanup_steps = 8

        # 1. Stop accepting new messages
        _log_cleanup_step(
            1, total_cleanup_steps, "Stopping message acceptance and polling"
        )
        shutdown.request_shutdown()
        channel.request_shutdown()

        if not poll_task.done():
            poll_task.cancel()
            try:
                await poll_task
            except asyncio.CancelledError:
                pass

        # 2. Wait for in-flight operations
        _log_cleanup_step(2, total_cleanup_steps, "Waiting for in-flight operations")
        cli_output.dim("  Waiting for in-flight operations to complete...")
        completed = await shutdown.wait_for_in_flight()
        if not completed:
            log.warning("Force proceeding after timeout")
            cli_output.warning("Timed out waiting for operations, forcing shutdown")

        # 3. Stop scheduler
        _log_cleanup_step(3, total_cleanup_steps, "Stopping task scheduler")
        cli_output.dim("  Stopping task scheduler...")
        try:
            await scheduler.stop()
        except Exception as e:
            log.warning("Error stopping scheduler: %s", e)

        # 4. Stop health check server
        if health_server:
            _log_cleanup_step(4, total_cleanup_steps, "Stopping health check server")
            cli_output.dim("  Stopping health check server...")
            try:
                await health_server.stop()
            except Exception as e:
                log.warning("Error stopping health server: %s", e)
        else:
            _log_cleanup_step(
                4, total_cleanup_steps, "Health check server not running, skipping"
            )

        # 5. Close channel
        _log_cleanup_step(5, total_cleanup_steps, "Closing channel connections")
        cli_output.dim("  Closing channel connections...")
        try:
            await channel.close()
        except Exception as e:
            log.warning("Error closing channel: %s", e)

        # 6. Close project store (flush WAL)
        _log_cleanup_step(6, total_cleanup_steps, "Closing project store")
        try:
            project_store.close()
        except Exception as e:
            log.warning("Error closing project store: %s", e)

        # 7. Close vector memory (flush WAL)
        _log_cleanup_step(7, total_cleanup_steps, "Closing vector memory")
        try:
            vector_memory.close()
        except Exception as e:
            log.warning("Error closing vector memory: %s", e)

        # 8. Close database
        _log_cleanup_step(8, total_cleanup_steps, "Closing database connections")
        cli_output.dim("  Closing database connections...")
        try:
            await db.close()
        except Exception as e:
            log.warning("Error closing database: %s", e)

        _log_shutdown_complete(shutdown_begin_time)
        cli_output.success("Shutdown complete.")


# ─────────────────────────────────────────────────────────────────────────────
# CLI Commands
# ─────────────────────────────────────────────────────────────────────────────


@click.group()
@click.option("--verbose", "-v", is_flag=True, default=False, help="Debug logging.")
@click.option(
    "--verbosity",
    type=click.Choice(["quiet", "normal", "verbose"], case_sensitive=False),
    default=None,
    help="Log verbosity level (overrides config.json).",
)
@click.option(
    "--log-format", "log_format", default=None, help="Log format: text or json"
)
@click.version_option(version=__version__, prog_name="custombot")
@click.pass_context
def cli(ctx, verbose, verbosity, log_format):
    """custombot — A lightweight WhatsApp AI assistant."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["log_format"] = log_format

    # Try to load config to get logging settings if not specified
    effective_format = log_format
    log_file = None
    log_max_bytes = 10 * 1024 * 1024
    log_backup_count = 5
    log_verbosity = "normal"

    if CONFIG_PATH.exists():
        try:
            cfg = load_config(CONFIG_PATH)
            if effective_format is None:
                effective_format = cfg.log_format
            log_file = cfg.log_file if cfg.log_file else None
            log_max_bytes = cfg.log_max_bytes
            log_backup_count = cfg.log_backup_count
            log_verbosity = cfg.log_verbosity
        except Exception:
            pass

    # CLI --verbosity overrides config; -v/--verbose is a shortcut for verbose
    if verbosity is not None:
        log_verbosity = verbosity
    if verbose:
        log_verbosity = "verbose"

    _setup_logging(
        verbose,
        log_format=effective_format or "text",
        log_file=log_file,
        log_max_bytes=log_max_bytes,
        log_backup_count=log_backup_count,
        log_verbosity=log_verbosity,
    )


@cli.command()
@click.option(
    "--config",
    "config_path",
    default=str(CONFIG_PATH),
    show_default=True,
    help="Path to config.json",
)
@click.option(
    "--health-port",
    "health_port",
    default=None,
    type=int,
    help="Port for health check HTTP server (disabled if not specified)",
)
@click.option(
    "--log-llm",
    "log_llm",
    is_flag=True,
    default=False,
    help="Log each LLM request and response to individual JSON files in workspace/logs/llm/",
)
@click.option(
    "--safe",
    "safe_mode",
    is_flag=True,
    default=False,
    help="Confirm every outgoing message before sending (Y/N prompt)",
)
@click.pass_context
def start(ctx, config_path, health_port, log_llm, safe_mode):
    """
    Start the bot and listen for WhatsApp messages.

    Connects to WhatsApp via neonize (native Python client).
    First run displays a QR code for pairing; subsequent runs
    auto-reconnect using the persisted session.

    \b
    Examples:
        python main.py start
        python main.py start --health-port 8080
        python main.py start --config my_config.json
        python main.py start --log-llm
    """
    log = logging.getLogger(__name__)
    config_file = Path(config_path)

    # Check config file exists
    if not config_file.exists():
        cli_output.error(f"Configuration file not found: {config_path}")
        cli_output.info("Run 'python main.py options' to create a configuration file.")
        sys.exit(1)

    # Load configuration
    try:
        cfg = load_config(config_file)
    except Exception as e:
        cli_output.error(f"Failed to load configuration: {e}")
        sys.exit(1)

    log.debug("Configuration file: %s", config_path)
    log.info("CustomBot starting...")

    # Warn if allowed_numbers is empty — bot will respond to anyone
    if not cfg.whatsapp.allowed_numbers:
        log.warning(
            "whatsapp.allowed_numbers is empty — bot will respond to ALL numbers. "
            "Set allowed_numbers in config.json to restrict access."
        )
        cli_output.dim(
            "  ⚠ No allowed_numbers set — bot responds to all numbers. "
            "Restrict by adding numbers to config.json."
        )

    # CLI --log-llm overrides config
    if log_llm:
        cfg.log_llm = True

    from src.dependency_check import check_dependencies

    check_dependencies(auto_update=True, critical_only=True)

    cli_output.bot(
        f"custombot starting...  provider={cfg.whatsapp.provider}  model={cfg.llm.model}"
    )

    try:
        asyncio.run(
            _run_bot(
                cfg,
                verbose=ctx.obj["verbose"],
                health_port=health_port,
                safe_mode=safe_mode,
            )
        )
    except FileNotFoundError as e:
        cli_output.raw("")
        cli_output.error(f"Startup failed: {e}")
        cli_output.info(
            "Create the missing instruction files in the 'workspace/instructions/' directory."
        )
        sys.exit(1)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.option(
    "--config",
    "config_path",
    default=str(CONFIG_PATH),
    show_default=True,
    help="Path to config.json",
)
def options(config_path):
    """
    Open the configuration editor (TUI).

    Provides an interactive menu for editing configuration settings:
    - LLM settings (api_key, model, base_url, temperature, max_tokens)
    - WhatsApp settings (provider, db_path)
    - General settings (workspace)

    \b
    Examples:
        python main.py options
        python main.py options --config my_config.json
    """
    from src.ui.options_tui import run_options_tui

    config_file = Path(config_path)

    # If config doesn't exist, create a default one first
    if not config_file.exists():
        cli_output.info(f"Configuration file not found at {config_path}")
        cli_output.info("Creating default configuration...")

        # Create default config
        default_cfg = Config(
            llm=LLMConfig(
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="",
            )
        )
        save_config(default_cfg, config_file)
        cli_output.success(f"Created default configuration at {config_path}")

    # Run the TUI
    try:
        run_options_tui(config_file)
    except KeyboardInterrupt:
        cli_output.info("Configuration editor cancelled.")
    except Exception as e:
        cli_output.error(f"Failed to run configuration editor: {e}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli(obj={})
