"""
exceptions.py — Custom exception hierarchy for CustomBot.

Provides domain-specific exceptions for different error types:
    - LLMError: LLM API communication failures
    - DatabaseError: Database operation failures
    - BridgeError: Bridge/transport-layer communication failures
    - ChannelError: Channel-specific errors (connection, auth, send failures)
    - SkillError: Skill execution failures
    - ConfigurationError: Configuration validation failures
    - RoutingError: Routing rule evaluation failures
    - DiskSpaceError: Insufficient disk space conditions
    - MemoryError: Vector memory and semantic search failures

Exception hierarchy::

    CustomBotException
    ├── LLMError
    ├── DatabaseError
    ├── BridgeError          (transport/protocol layer)
    ├── ChannelError         (channel-specific: connection, auth, send)
    ├── SkillError
    ├── ConfigurationError
    │   └── ConfigValidationError
    ├── RoutingError
    ├── DiskSpaceError
    └── MemoryError          (vector search, embedding failures)

BridgeError vs ChannelError:
    ``BridgeError`` covers transport-layer and protocol errors (e.g. the
    WhatsApp bridge process is unreachable, message serialization fails).
    ``ChannelError`` covers channel-specific errors that are tied to a
    particular channel implementation (e.g. WhatsApp auth failure, QR
    timeout, connection drop during send).  When in doubt, prefer
    ``ChannelError`` for errors originating in ``src/channels/`` and
    ``BridgeError`` for errors in the bridge/transport layer.

All exceptions support:
    - User-friendly messages with actionable suggestions
    - Error codes for support reference
    - Documentation links for self-service help
    - Consistent emoji formatting for visual clarity

Usage:
    from src.exceptions import LLMError, DatabaseError, format_user_error

    raise LLMError("API timeout", provider="openai", model="gpt-4")
    raise DatabaseError("Connection failed", operation="save_message")

    # Format for user display:
    user_msg = format_user_error(exception)
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """Error codes for support reference and programmatic handling."""

    # LLM errors (1000-1099)
    LLM_API_KEY_INVALID = "ERR_1001"
    LLM_RATE_LIMITED = "ERR_1002"
    LLM_TIMEOUT = "ERR_1003"
    LLM_MODEL_UNAVAILABLE = "ERR_1004"
    LLM_CONNECTION_FAILED = "ERR_1005"
    LLM_INVALID_REQUEST = "ERR_1006"
    LLM_CONTEXT_LENGTH_EXCEEDED = "ERR_1007"
    LLM_CIRCUIT_BREAKER_OPEN = "ERR_1008"

    # Bridge/Channel errors (2000-2099)
    BRIDGE_CONNECTION_FAILED = "ERR_2001"
    BRIDGE_AUTH_FAILED = "ERR_2002"
    BRIDGE_NOT_RUNNING = "ERR_2003"
    BRIDGE_TIMEOUT = "ERR_2004"

    # Database errors (3000-3099)
    DB_CONNECTION_FAILED = "ERR_3001"
    DB_WRITE_FAILED = "ERR_3002"
    DB_READ_FAILED = "ERR_3003"

    # Skill errors (4000-4099)
    SKILL_NOT_FOUND = "ERR_4001"
    SKILL_EXECUTION_FAILED = "ERR_4002"
    SKILL_TIMEOUT = "ERR_4003"
    SKILL_PERMISSION_DENIED = "ERR_4004"

    # Configuration errors (5000-5099)
    CONFIG_MISSING = "ERR_5001"
    CONFIG_INVALID = "ERR_5002"

    # Routing errors (6000-6099)
    ROUTING_INVALID_PATTERN = "ERR_6001"
    ROUTING_INSTRUCTION_NOT_FOUND = "ERR_6002"

    # Generic errors (9000-9099)
    UNKNOWN = "ERR_9000"


# Documentation URLs for common error categories
_BASE_DOCS = "https://github.com/pierrecorsini/custombot"
DOCS_URLS: dict[str, str | None] = {
    "llm": f"{_BASE_DOCS}#llm-providers",
    "bridge": f"{_BASE_DOCS}#quick-start",
    "database": f"{_BASE_DOCS}#workspace-isolation",
    "skills": f"{_BASE_DOCS}#built-in-skills",
    "config": f"{_BASE_DOCS}#configuration-configjson",
    "general": _BASE_DOCS,
}


class CustomBotException(Exception):
    """
    Base exception for all CustomBot errors.

    All domain-specific exceptions inherit from this class, enabling
    broad exception catching when needed while still allowing specific
    error handling.

    Attributes:
        message: Human-readable error description
        details: Optional dict with additional context (e.g., provider, operation)
        suggestion: Actionable suggestion for how to resolve the error
        error_code: Unique code for support reference
        docs_url: Link to relevant documentation
    """

    default_message = "An unexpected error occurred in CustomBot"
    default_suggestion: Optional[str] = None
    default_error_code = ErrorCode.UNKNOWN
    default_docs_category: Optional[str] = None

    def __init__(
        self,
        message: Optional[str] = None,
        suggestion: Optional[str] = None,
        error_code: Optional[ErrorCode] = None,
        docs_url: Optional[str] = None,
        **details: Any,
    ) -> None:
        self.message = message or self.default_message
        self.suggestion = suggestion or self.default_suggestion
        self.error_code = error_code or self.default_error_code
        self.details = details

        # Set docs_url from category if not explicitly provided
        if docs_url:
            self.docs_url = docs_url
        elif self.default_docs_category and self.default_docs_category in DOCS_URLS:
            self.docs_url = DOCS_URLS[self.default_docs_category]
        else:
            self.docs_url = DOCS_URLS.get("general")

        super().__init__(self.message)

    def __str__(self) -> str:
        """Return a nicely formatted error message with context."""
        if self.details:
            detail_str = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
            return f"{self.message} ({detail_str})"
        return self.message

    def __repr__(self) -> str:
        """Return a developer-friendly representation."""
        class_name = self.__class__.__name__
        parts = [f"message={self.message!r}"]
        if self.suggestion:
            parts.append(f"suggestion={self.suggestion!r}")
        if self.error_code != ErrorCode.UNKNOWN:
            parts.append(f"error_code={self.error_code}")
        if self.details:
            detail_str = ", ".join(f"{k}={v!r}" for k, v in self.details.items())
            parts.append(f"details={{{detail_str}}}")
        return f"{class_name}({', '.join(parts)})"

    def to_user_message(
        self,
        include_ref: bool = True,
        correlation_id: Optional[str] = None,
        include_docs: bool = True,
    ) -> str:
        """
        Format the error as a user-friendly message.

        Args:
            include_ref: Whether to include error code reference
            correlation_id: Optional correlation ID for request tracing
            include_docs: Whether to include documentation links

        Returns:
            Formatted message with emoji, explanation, and suggestions.
        """
        parts = []

        # Main error message with emoji
        parts.append(f"⚠️ {self.message}")

        # Add actionable suggestion
        if self.suggestion:
            parts.append(f"💡 {self.suggestion}")

        # Add error code and/or correlation ID for support reference
        if include_ref and self.error_code != ErrorCode.UNKNOWN:
            ref_parts = [f"🔢 Ref: {self.error_code.value}"]
            if correlation_id:
                ref_parts.append(correlation_id)
            parts.append(" | ".join(ref_parts))
        elif correlation_id:
            parts.append(f"🔢 Ref: {correlation_id}")

        # Add documentation link
        if include_docs and self.docs_url:
            parts.append(f"📚 Help: {self.docs_url}")

        return "\n".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Domain-Specific Exceptions
# ─────────────────────────────────────────────────────────────────────────────


class LLMError(CustomBotException):
    """
    Exception for LLM API communication failures.

    Raised when:
        - API request times out
        - API returns an error response
        - Model is unavailable
        - Rate limiting occurs
        - Authentication fails

    Example:
        raise LLMError("API timeout", provider="openai", model="gpt-4")
    """

    default_message = "LLM API request failed"
    default_docs_category = "llm"


class DatabaseError(CustomBotException):
    """
    Exception for database operation failures.

    Raised when:
        - File read/write operations fail
        - Data corruption is detected
        - Concurrent access issues occur
        - Index rebuilding fails

    Example:
        raise DatabaseError("Failed to save message", operation="save_message", chat_id="123")
    """

    default_message = "Database operation failed"
    default_docs_category = "database"


class BridgeError(CustomBotException):
    """
    Exception for bridge/channel communication failures.

    Raised when:
        - Message sending fails
        - Connection to channel is lost
        - Authentication with channel provider fails
        - Message format is invalid

    Example:
        raise BridgeError("Failed to send message", channel="whatsapp", recipient="+1234567890")
    """

    default_message = "Bridge communication failed"
    default_docs_category = "bridge"


class SkillError(CustomBotException):
    """
    Exception for skill execution failures.

    Raised when:
        - Skill execution raises an exception
        - Required skill is not found
        - Skill arguments are invalid
        - Workspace access fails

    Example:
        raise SkillError("Skill execution failed", skill="read_file", reason="Permission denied")
    """

    default_message = "Skill execution failed"
    default_docs_category = "skills"


class ConfigurationError(CustomBotException):
    """
    Exception for configuration validation failures.

    Raised when:
        - Required configuration is missing
        - Configuration values are invalid
        - Environment variables are not set
        - File paths are invalid

    Example:
        raise ConfigurationError("Missing API key", config_key="llm.api_key")
    """

    default_message = "Configuration error"
    default_docs_category = "config"


class RoutingError(CustomBotException):
    """
    Exception for routing rule evaluation failures.

    Raised when:
        - Routing rule pattern is invalid
        - Instruction file is not found
        - Rule evaluation raises an exception

    Example:
        raise RoutingError("Invalid regex pattern", rule_id="rule-123", pattern="[invalid")
    """

    default_message = "Routing error"


class DiskSpaceError(CustomBotException):
    """
    Exception for insufficient disk space conditions.

    Raised when:
        - Disk space check fails before write operations
        - Available space is below minimum threshold
        - Disk is full or nearly full

    Example:
        raise DiskSpaceError("Insufficient disk space", path="/data", free_mb=50, required_mb=100)
    """

    default_message = "Insufficient disk space"


class ChannelError(CustomBotException):
    """
    Exception for channel-specific errors (connection, auth, send failures).

    ``ChannelError`` covers errors originating in channel implementations
    (``src/channels/``) that are tied to a specific channel type, such as
    WhatsApp connection drops, QR timeout, or authentication failures.

    Contrast with ``BridgeError``, which covers transport-layer and
    protocol errors (e.g. the bridge process is unreachable, message
    serialization fails).  When in doubt, prefer ``ChannelError`` for
    errors originating in ``src/channels/`` and ``BridgeError`` for
    errors in the bridge/transport layer.

    Example:
        raise ChannelError("WhatsApp connection lost", channel="whatsapp", reason="timeout")
    """

    default_message = "Channel communication failed"
    default_docs_category = "bridge"


class MemoryError(CustomBotException):
    """
    Exception for vector memory and semantic search failures.

    Raised when:
        - Vector search fails (embedding API error, index corruption)
        - Memory overflow during embedding computation
        - Vector store connection or query errors

    Example:
        raise MemoryError("Vector search failed", operation="semantic_search", query="hello")
    """

    default_message = "Memory operation failed"
    default_docs_category = "database"


# ─────────────────────────────────────────────────────────────────────────────
# Error Factory Functions
# ─────────────────────────────────────────────────────────────────────────────


def create_api_key_error(provider: str = "your provider") -> LLMError:
    """Create a user-friendly API key invalid error."""
    return LLMError(
        message="Your API key is invalid or not set",
        suggestion=f"Check your API key at your provider's dashboard. "
        f"For OpenAI: https://platform.openai.com/api-keys",
        error_code=ErrorCode.LLM_API_KEY_INVALID,
        provider=provider,
    )


def create_rate_limit_error(retry_after: Optional[int] = None) -> LLMError:
    """Create a user-friendly rate limit error."""
    suggestion = "Wait a moment and try again."
    if retry_after:
        suggestion = f"Wait {retry_after} seconds before trying again."

    return LLMError(
        message="Too many requests - you've been rate limited",
        suggestion=suggestion,
        error_code=ErrorCode.LLM_RATE_LIMITED,
    )


def create_connection_error(service: str = "service") -> BridgeError:
    """Create a user-friendly connection error for WhatsApp/LLM services."""
    return BridgeError(
        message=f"Could not connect to {service}",
        suggestion="Check that your configuration is correct and try again",
        error_code=ErrorCode.BRIDGE_CONNECTION_FAILED,
        service=service,
    )


def create_bridge_not_running_error() -> BridgeError:
    """Create a user-friendly WhatsApp not connected error."""
    return BridgeError(
        message="WhatsApp is not connected",
        suggestion="The neonize client could not establish a connection. Check your session database and try again.",
        error_code=ErrorCode.BRIDGE_NOT_RUNNING,
    )


def create_skill_timeout_error(skill_name: str, timeout: int) -> SkillError:
    """Create a user-friendly skill timeout error."""
    return SkillError(
        message=f"The '{skill_name}' operation took too long",
        suggestion=f"The operation timed out after {timeout}s. "
        "Try breaking your request into smaller parts.",
        error_code=ErrorCode.SKILL_TIMEOUT,
        skill=skill_name,
        timeout=timeout,
    )


def create_skill_not_found_error(skill_name: str) -> SkillError:
    """Create a user-friendly skill not found error."""
    return SkillError(
        message=f"Skill '{skill_name}' not found",
        suggestion="Check the skill name or run 'python main.py skills list' to see available skills.",
        error_code=ErrorCode.SKILL_NOT_FOUND,
        skill=skill_name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Formatting Helpers
# ─────────────────────────────────────────────────────────────────────────────


def format_user_error(
    error: Exception,
    correlation_id: Optional[str] = None,
    include_docs: bool = True,
) -> str:
    """
    Format any exception as a user-friendly error message.

    For CustomBotException instances, delegates to ``to_user_message()`` to
    keep formatting logic in a single place. Falls back to a generic message
    for standard Python exceptions.

    Args:
        error: The exception to format
        correlation_id: Optional correlation ID for request tracing
        include_docs: Whether to include documentation links

    Returns:
        Formatted user-friendly error message with emoji
    """
    if isinstance(error, CustomBotException):
        return error.to_user_message(
            include_ref=True,
            correlation_id=correlation_id,
            include_docs=include_docs,
        )

    # Handle non-CustomBot exceptions with a generic message
    parts = ["⚠️ An unexpected error occurred"]

    if correlation_id:
        parts.append(f"🔢 Ref: {correlation_id}")

    parts.append("Please try again. If the problem persists, contact support.")

    return "\n".join(parts)


def get_user_friendly_message(technical_error: str, error_type: str) -> str:
    """
    Convert technical error types into user-friendly explanations.

    Args:
        technical_error: The technical error message
        error_type: The exception type name

    Returns:
        A user-friendly explanation of the error
    """
    # Map common error types to user-friendly messages
    friendly_messages = {
        "validation": "The input provided was not valid.",
        "ValidationError": "The input provided was not valid.",
        "PermissionError": "Permission denied - I can't access that.",
        "FileNotFoundError": "The requested file or resource was not found.",
        "TimeoutError": "The operation took too long to complete.",
        "ValueError": "The input format was unexpected.",
        "KeyError": "A required piece of information was missing.",
        "TypeError": "The data format was unexpected.",
        "ConnectionError": "Could not connect to the service.",
        "RateLimitError": "Too many requests - please wait a moment.",
    }

    # Check for specific error types first
    if error_type in friendly_messages:
        return friendly_messages[error_type]

    # Generic fallback — never expose raw technical_error text to users
    return "An error occurred while processing your request."


# ─────────────────────────────────────────────────────────────────────────────
# Convenience Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # Base exception
    "CustomBotException",
    # Domain exceptions
    "LLMError",
    "DatabaseError",
    "BridgeError",
    "ChannelError",
    "SkillError",
    "ConfigurationError",
    "RoutingError",
    "DiskSpaceError",
    "MemoryError",
    # Error codes
    "ErrorCode",
    # Factory functions
    "create_api_key_error",
    "create_rate_limit_error",
    "create_connection_error",
    "create_bridge_not_running_error",
    "create_skill_timeout_error",
    "create_skill_not_found_error",
    # Formatting helpers
    "format_user_error",
    "get_user_friendly_message",
]
