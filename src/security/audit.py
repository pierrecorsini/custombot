"""
src/security/audit.py — Structured audit logging for security-relevant events.

Provides a shared audit logging function used by file, shell, and other
skill modules to record operations in a consistent, parseable format.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("security.audit")


def audit_log(
    event: str,
    details: dict[str, Any],
    *,
    level: int = logging.WARNING,
    prefix: str = "AUDIT",
) -> None:
    """
    Log a structured audit event.

    Args:
        event: Event type (e.g., "file_read", "command_blocked", "path_blocked").
        details: Additional context about the event.
        level: Logging level (default WARNING for security events).
        prefix: Prefix for the log message (e.g., "FILE_AUDIT", "SECURITY_AUDIT").
    """
    log.log(
        level,
        "%s: %s | %s",
        prefix,
        event,
        " | ".join(f"{k}={v}" for k, v in details.items()),
        extra={f"audit_{prefix.lower()}": event, **details},
    )
