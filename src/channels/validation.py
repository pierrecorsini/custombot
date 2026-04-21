"""
src/channels/validation.py — Channel validation before starting the bot.

Validates LLM API credentials and WhatsApp (neonize) configuration before
the bot starts, providing detailed error messages for troubleshooting.

Usage:
    from src.channels.validation import validate_channels, validate_all_channels

    # Simple validation (returns success + error list)
    success, errors = await validate_channels(config)
    if not success:
        for error in errors:
            print(error)

    # Detailed validation (returns ValidationResult for each channel)
    results = await validate_all_channels(config)
    for result in results:
        print(f"{result.channel}: {result.message}")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from src.health import check_llm_credentials
from src.ui.cli_output import cli

if TYPE_CHECKING:
    from src.config import Config

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ValidationResult:
    """Result of validating a single channel."""

    channel: str  # "llm" or "whatsapp"
    success: bool
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "channel": self.channel,
            "success": self.success,
            "message": self.message,
            "details": self.details,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Validation Functions
# ─────────────────────────────────────────────────────────────────────────────


async def _validate_llm(config: "Config") -> ValidationResult:
    """Validate LLM API credentials."""
    api_key = config.llm.api_key
    base_url = config.llm.base_url
    model = config.llm.model

    if not api_key:
        message = "LLM API key is not configured"
        cli.error(message)
        return ValidationResult(
            channel="llm",
            success=False,
            message=message,
            details={
                "hint": "Set llm.api_key in config.json or LLM_API_KEY environment variable",
                "base_url": base_url,
                "model": model,
            },
        )

    placeholder_patterns = ["sk-your", "your-api-key", "your_api_key", "xxx"]
    if any(pattern in api_key.lower() for pattern in placeholder_patterns):
        message = "LLM API key appears to be a placeholder value"
        cli.error(message)
        return ValidationResult(
            channel="llm",
            success=False,
            message=message,
            details={
                "hint": "Replace the placeholder with your actual API key",
                "base_url": base_url,
                "model": model,
            },
        )

    cli.loading(f"Verifying LLM credentials ({model})...")
    health = await check_llm_credentials(api_key=api_key, base_url=base_url)

    if health.status.value == "healthy":
        message = f"LLM API credentials verified ({model})"
        cli.success(message)
        return ValidationResult(
            channel="llm",
            success=True,
            message=message,
            details={
                "model": model,
                "base_url": base_url,
                "latency_ms": health.latency_ms,
            },
        )

    if health.status.value == "degraded":
        message = f"LLM API check timed out, but credentials may be valid ({model})"
        cli.warning(message)
        return ValidationResult(
            channel="llm",
            success=True,
            message=message,
            details={
                "model": model,
                "base_url": base_url,
                "latency_ms": health.latency_ms,
                "warning": health.message,
            },
        )

    message = f"LLM API credentials are invalid: {health.message}"
    cli.error(message)
    return ValidationResult(
        channel="llm",
        success=False,
        message=message,
        details={
            "hint": "Check that your API key is correct and has not expired",
            "model": model,
            "base_url": base_url,
            "error": health.message,
        },
    )


async def _validate_whatsapp(config: "Config") -> ValidationResult:
    """
    Validate WhatsApp (neonize) configuration.

    Checks that the neonize db_path is writable (parent directory exists
    or can be created). Actual connection happens at channel start time.
    """
    neonize_cfg = config.whatsapp.neonize
    db_path = Path(neonize_cfg.db_path)

    # Verify provider is neonize
    if config.whatsapp.provider != "neonize":
        message = f"Unsupported WhatsApp provider: {config.whatsapp.provider!r}"
        cli.error(message)
        return ValidationResult(
            channel="whatsapp",
            success=False,
            message=message,
            details={
                "hint": "Only 'neonize' provider is supported",
                "provider": config.whatsapp.provider,
            },
        )

    # Check db_path is non-empty
    if not neonize_cfg.db_path:
        message = "WhatsApp session db_path is not configured"
        cli.error(message)
        return ValidationResult(
            channel="whatsapp",
            success=False,
            message=message,
            details={
                "hint": "Set whatsapp.neonize.db_path in config.json",
                "default": "workspace/whatsapp_session.db",
            },
        )

    # Verify parent directory is writable
    cli.loading("Checking WhatsApp (neonize) configuration...")
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        message = "WhatsApp (neonize) configuration valid"
        cli.success(message)
        return ValidationResult(
            channel="whatsapp",
            success=True,
            message=message,
            details={"db_path": str(db_path)},
        )
    except OSError as e:
        message = f"WhatsApp session db_path not writable: {e}"
        cli.error(message)
        return ValidationResult(
            channel="whatsapp",
            success=False,
            message=message,
            details={
                "hint": "Ensure the directory for db_path exists and is writable",
                "db_path": str(db_path),
                "error": str(e),
            },
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main Validation Functions
# ─────────────────────────────────────────────────────────────────────────────


async def validate_all_channels(config: "Config") -> list[ValidationResult]:
    """Validate all channels required for bot operation."""
    results: list[ValidationResult] = []
    results.append(await _validate_llm(config))
    results.append(await _validate_whatsapp(config))
    return results


async def validate_channels(config: "Config") -> tuple[bool, list[str]]:
    """
    Validate all channels and return simple success/errors tuple.

    Returns:
        Tuple of (success: bool, errors: list[str]).
    """
    results = await validate_all_channels(config)
    errors: list[str] = []
    all_success = True

    for result in results:
        if not result.success:
            all_success = False
            error_msg = f"[{result.channel.upper()}] {result.message}"
            if "hint" in result.details:
                error_msg += f"\n  Hint: {result.details['hint']}"
            errors.append(error_msg)
            log.error(
                "Channel validation failed: %s - %s",
                result.channel,
                result.message,
            )

    return all_success, errors


# ─────────────────────────────────────────────────────────────────────────────
# Utility Functions
# ─────────────────────────────────────────────────────────────────────────────


def format_validation_report(results: list[ValidationResult]) -> str:
    """Format validation results as a human-readable report."""
    lines = ["Channel Validation Report", "=" * 40]

    for result in results:
        status = "✓ PASS" if result.success else "✗ FAIL"
        lines.append(f"\n[{status}] {result.channel.upper()}")
        lines.append(f"  {result.message}")

        if result.details.get("latency_ms") is not None:
            lines.append(f"  Latency: {result.details['latency_ms']:.2f}ms")

        if result.details.get("hint"):
            lines.append(f"  Hint: {result.details['hint']}")

        if result.details.get("warning"):
            lines.append(f"  Warning: {result.details['warning']}")

    passed = sum(1 for r in results if r.success)
    total = len(results)
    lines.append(f"\n{'=' * 40}")
    lines.append(f"Summary: {passed}/{total} channels passed")

    return "\n".join(lines)
