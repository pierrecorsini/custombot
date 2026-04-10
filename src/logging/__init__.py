"""
src/logging — Structured logging package with JSON format support.

Provides:
  - setup_logging: Configure logging with text or JSON format
  - VerbosityLevel: Logging verbosity levels
  - Correlation ID support for request tracing
  - Sensitive data redaction
"""

from src.logging.logging_config import (
    setup_logging,
    get_verbosity,
    set_verbosity,
    VerbosityLevel,
    get_correlation_id,
    set_correlation_id,
    clear_correlation_id,
    new_correlation_id,
    get_sensitive_patterns,
    add_sensitive_pattern,
    remove_sensitive_pattern,
    redact_sensitive,
    JsonFormatter,
    SensitiveFormatter,
)
from src.logging.http_logging import (
    log_request,
    log_response,
    log_request_response,
)

__all__ = [
    # Setup
    "setup_logging",
    # Verbosity
    "get_verbosity",
    "set_verbosity",
    "VerbosityLevel",
    # Correlation IDs
    "get_correlation_id",
    "set_correlation_id",
    "clear_correlation_id",
    "new_correlation_id",
    # Sensitive data
    "get_sensitive_patterns",
    "add_sensitive_pattern",
    "remove_sensitive_pattern",
    "redact_sensitive",
    # Formatters
    "JsonFormatter",
    "SensitiveFormatter",
    # HTTP logging
    "log_request",
    "log_response",
    "log_request_response",
]
