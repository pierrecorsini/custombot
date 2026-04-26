"""
src/logging/http_logging.py — Shared HTTP request/response logging utilities.

Provides redaction of sensitive fields, correlation ID tracking,
and structured logging for HTTP requests and responses.
Used by LLM client and channels for structured HTTP logging.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional

import httpx

from src.core.errors import NonCriticalCategory, log_noncritical

# Sensitive field patterns to redact in logs
SENSITIVE_FIELDS = frozenset(
    {
        "api_key",
        "apiKey",
        "api-key",
        "auth",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "pass",
        "secret",
        "credential",
        "session",
        "session_id",
        "sessionId",
        "private_key",
        "privateKey",
        "authorization",
        "x-api-key",
    }
)

SENSITIVE_PATTERNS = [
    re.compile(r"(api[_-]?key|token|secret|password|auth)", re.IGNORECASE),
]


def _should_log_http_requests() -> bool:
    """Check if HTTP requests should be logged at INFO level (verbose mode only)."""
    try:
        from src.logging.logging_config import VerbosityLevel, get_verbosity

        return get_verbosity() == VerbosityLevel.VERBOSE
    except ImportError:
        return False


def redact_sensitive_data(data: Any, max_depth: int = 5) -> Any:
    """
    Recursively redact sensitive fields from data structures.

    Returns a redacted copy with sensitive values replaced by '***REDACTED***'.
    """
    if max_depth <= 0:
        return "[MAX_DEPTH_EXCEEDED]"

    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            key_lower = key.lower().replace("-", "_")
            if key_lower in SENSITIVE_FIELDS or key in SENSITIVE_FIELDS:
                redacted[key] = "***REDACTED***"
            elif any(p.search(str(key)) for p in SENSITIVE_PATTERNS):
                redacted[key] = "***REDACTED***"
            else:
                redacted[key] = redact_sensitive_data(value, max_depth - 1)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_data(item, max_depth - 1) for item in data]
    elif isinstance(data, str) and len(data) > 200:
        return data[:200] + "...[TRUNCATED]"
    return data


def get_correlation_id() -> str:
    """Get or create a correlation ID for request tracing."""
    try:
        from src.logging.logging_config import (
            get_correlation_id as _get,
        )
        from src.logging.logging_config import (
            new_correlation_id,
            set_correlation_id,
        )

        corr_id = _get()
        if not corr_id:
            corr_id = new_correlation_id()
            set_correlation_id(corr_id)
        return corr_id
    except ImportError:
        import uuid

        return str(uuid.uuid4())[:8]


def format_http_error(error: Exception) -> str:
    """Format an HTTP exception into a descriptive message for logging."""
    if isinstance(error, httpx.TimeoutException):
        return f"Request timed out ({type(error).__name__})"
    if isinstance(error, httpx.ConnectError):
        return "Connection refused - bridge server not reachable"
    if isinstance(error, httpx.HTTPStatusError):
        body = error.response.text[:100] if error.response.text else ""
        return f"HTTP {error.response.status_code}: {body}"
    if isinstance(error, httpx.HTTPError):
        return f"HTTP error ({type(error).__name__})"
    error_str = str(error)
    return error_str if error_str else f"{type(error).__name__} (no details)"


def log_request(
    logger: logging.Logger,
    method: str,
    url: str,
    *,
    tag: str = "BRIDGE",
    body: Optional[dict] = None,
    debug_mode: bool = False,
    level: int = logging.INFO,
) -> str:
    """
    Log an outgoing HTTP request. Returns the correlation ID.
    """
    corr_id = get_correlation_id()

    # Only log at INFO level in verbose mode, otherwise DEBUG
    effective_level = level if _should_log_http_requests() else logging.DEBUG

    logger.log(
        effective_level,
        "[%s] --> %s %s [corr: %s]",
        tag,
        method.upper(),
        url,
        corr_id,
    )

    if debug_mode and body is not None:
        logger.debug(
            "[%s] Request body [corr: %s]: %s",
            tag,
            corr_id,
            redact_sensitive_data(body),
        )

    return corr_id


def log_response(
    logger: logging.Logger,
    corr_id: str,
    method: str,
    url: str,
    status_code: int,
    duration_ms: float,
    *,
    tag: str = "BRIDGE",
    body: Optional[Any] = None,
    error: Optional[Exception] = None,
    debug_mode: bool = False,
    level: int = logging.INFO,
) -> None:
    """Log an HTTP response (success or failure)."""
    if error:
        error_msg = format_http_error(error)
        logger.warning(
            "[%s] <-- %s %s FAILED [corr: %s] (%.1fms): %s",
            tag,
            method.upper(),
            url,
            corr_id,
            duration_ms,
            error_msg,
        )
        if debug_mode and hasattr(error, "response"):
            try:
                error_body = getattr(error.response, "text", None)
                if error_body:
                    logger.debug(
                        "[%s] Error response [corr: %s]: %s",
                        tag,
                        corr_id,
                        error_body[:500],
                    )
            except Exception:
                log_noncritical(
                    NonCriticalCategory.LOGGING,
                    "Failed to extract error response body for %s %s [corr: %s]",
                    method.upper(),
                    url,
                    corr_id,
                    logger=logger,
                )
        # Only log at INFO level in verbose mode, otherwise DEBUG
        effective_level = level if _should_log_http_requests() else logging.DEBUG

        logger.log(
            effective_level,
            "[%s] <-- %s %s %d [corr: %s] (%.1fms)",
            tag,
            method.upper(),
            url,
            status_code,
            corr_id,
            duration_ms,
        )
        if debug_mode and body is not None:
            logger.debug(
                "[%s] Response body [corr: %s]: %s",
                tag,
                corr_id,
                redact_sensitive_data(body),
            )


def log_request_response(
    logger: logging.Logger,
    method: str,
    url: str,
    status_code: int,
    duration_ms: float,
    *,
    request_body: Optional[dict] = None,
    response_body: Optional[Any] = None,
    error: Optional[Exception] = None,
    tag: str = "BRIDGE",
    debug_mode: bool = False,
) -> None:
    """
    Log both request and response in a single call.
    Convenience function for simple logging scenarios.
    """
    corr_id = log_request(logger, method, url, body=request_body, debug_mode=debug_mode)
    log_response(
        logger,
        corr_id,
        method,
        url,
        status_code,
        duration_ms,
        body=response_body,
        error=error,
        debug_mode=debug_mode,
    )
