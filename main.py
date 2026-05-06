"""
main.py — CLI entry point for custombot.

Commands:
  start     Start the bot (connects to WhatsApp via neonize)
  options   Open configuration editor (TUI)
  diagnose  Run diagnostic checks and output a report

Usage:
  python main.py start     # Start the bot
  python main.py options   # Edit configuration
  python main.py diagnose  # Troubleshoot common issues
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Optional

# Fix Windows console encoding for Unicode (emojis, special chars)
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import click

from src.__version__ import __version__
from src.app import Application
from src.config import (
    CONFIG_PATH,
    Config,
    LLMConfig,
    load_config,
    save_config,
)
from src.ui.cli_output import cli as cli_output

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
    from src.logging.logging_config import VerbosityLevel, setup_logging

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
    health_host: str = "127.0.0.1",
    safe_mode: bool = False,
) -> None:
    """Run the bot with all components and graceful shutdown handling."""
    app = Application(
        config,
        verbose=verbose,
        health_port=health_port,
        health_host=health_host,
        safe_mode=safe_mode,
    )
    await app.run()


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
@click.option("--log-format", "log_format", default=None, help="Log format: text or json")
@click.version_option(version=__version__, prog_name="custombot")
@click.pass_context
def cli(ctx, verbose, verbosity, log_format):
    """custombot — A lightweight WhatsApp AI assistant."""
    ctx.ensure_object(dict)
    ctx.obj["verbose"] = verbose
    ctx.obj["log_format"] = log_format
    ctx.obj["config"] = None

    # Try to load config to get logging settings if not specified
    effective_format = log_format
    log_file = None
    log_max_bytes = 10 * 1024 * 1024
    log_backup_count = 5
    log_verbosity = "normal"

    if CONFIG_PATH.exists():
        try:
            cfg = load_config(CONFIG_PATH)
            ctx.obj["config"] = cfg
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
    "--health-host",
    "health_host",
    default="127.0.0.1",
    show_default=True,
    help="Host/IP to bind the health check server to. Use 0.0.0.0 to expose to all interfaces.",
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
def start(ctx, config_path, health_port, health_host, log_llm, safe_mode):
    """
    Start the bot and listen for WhatsApp messages.

    Connects to WhatsApp via neonize (native Python client).
    First run displays a QR code for pairing; subsequent runs
    auto-reconnect using the persisted session.

    \b
    Examples:
        python main.py start
        python main.py start --health-port 8080
        python main.py start --health-port 8080 --health-host 0.0.0.0
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

    # Warn if config file containing API keys has overly permissive permissions
    if config_file.exists():
        try:
            stat_result = config_file.stat()
            # Check if group or others have read permission (Unix)
            if sys.platform != "win32" and (stat_result.st_mode & 0o047):
                log.error(
                    "SECURITY: Config file %s has overly permissive permissions (mode=%o). "
                    "Local users can read your API key. Fix: chmod 600 %s",
                    config_path,
                    stat_result.st_mode & 0o777,
                    config_path,
                )
                cli_output.error(
                    f"  ⚠ Config file is readable by others — run: chmod 600 {config_path}"
                )
        except OSError:
            pass

    # Load configuration — reuse cached config from CLI group when same path
    cfg = None
    if config_file == CONFIG_PATH:
        cfg = ctx.obj.get("config")
    if cfg is None:
        try:
            cfg = load_config(config_file)
        except Exception as e:
            cli_output.error(f"Failed to load configuration: {e}")
            sys.exit(1)

    log.debug("Configuration file: %s", config_path)
    log.info("CustomBot starting...")

    # Warn if allowed_numbers is empty and allow_all is not set — bot won't respond
    if not cfg.whatsapp.allowed_numbers and not cfg.whatsapp.allow_all:
        log.warning(
            "whatsapp.allowed_numbers is empty AND allow_all is False — "
            "bot will NOT respond to any numbers. "
            "Set allowed_numbers in config.json or set allow_all=true for development."
        )
        cli_output.dim(
            "  ⚠ No allowed_numbers set and allow_all=false — bot will NOT respond. "
            "Add numbers to config.json or set allow_all=true."
        )
    elif not cfg.whatsapp.allowed_numbers and cfg.whatsapp.allow_all:
        log.warning(
            "whatsapp.allowed_numbers is empty but allow_all=True — bot will respond to ALL numbers."
        )
        cli_output.dim(
            "  ⚠ No allowed_numbers set — bot responds to ALL numbers (allow_all=true). "
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
                health_host=health_host,
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
@click.option("--cleanup", is_flag=True, default=False, help="Remove orphaned workspace dirs")
def diagnose(config_path, cleanup):
    """
    Run diagnostic checks and output a structured report.

    Checks config validity, LLM connectivity, workspace integrity,
    disk space, and dependency status. Useful for troubleshooting
    before filing issues.

    \b
    Examples:
        python main.py diagnose
        python main.py diagnose --config my_config.json
        python main.py diagnose --cleanup
    """
    from src.diagnose import run_diagnose_cli

    run_diagnose_cli(Path(config_path), cleanup=cleanup)


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
