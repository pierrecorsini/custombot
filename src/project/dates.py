"""Shared date formatting utilities for the project module."""

from datetime import datetime, timezone


def fmt_ts(epoch: float) -> str:
    """Format an epoch timestamp as a human-readable UTC string."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
