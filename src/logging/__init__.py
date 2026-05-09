"""
src/logging — Structured logging package with JSON format support.

Provides:
  - setup_logging: Configure logging with text or JSON format
  - VerbosityLevel: Logging verbosity levels
  - Correlation ID support for request tracing
  - Structured context (chat_id, app_phase, session_id) auto-injection
  - Sensitive data redaction
"""

from src.logging.http_logging import (
    log_request,
    log_request_response,
    log_response,
)
from src.logging.logging_config import (
    CorrelationIdFilter,
    JsonFormatter,
    SensitiveFormatter,
    StructuredContextFilter,
    VerbosityLevel,
    add_sensitive_pattern,
    clear_correlation_id,
    clear_log_context,
    correlation_id_scope,
    get_app_phase,
    get_chat_id,
    get_correlation_id,
    get_sensitive_patterns,
    get_session_id,
    get_verbosity,
    new_correlation_id,
    redact_sensitive,
    remove_sensitive_pattern,
    set_app_phase,
    set_chat_id,
    set_correlation_id,
    set_log_context,
    set_session_id,
    set_verbosity,
    setup_logging,
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
    "correlation_id_scope",
    # Structured context
    "get_chat_id",
    "set_chat_id",
    "get_app_phase",
    "set_app_phase",
    "get_session_id",
    "set_session_id",
    "set_log_context",
    "clear_log_context",
    "StructuredContextFilter",
    "CorrelationIdFilter",
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
