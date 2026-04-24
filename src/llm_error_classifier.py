"""
llm_error_classifier.py — Map OpenAI SDK exceptions to domain LLMError instances.

Extracted from llm.py so that:
  1. llm.py stays focused on the client.
  2. The classifier can be unit-tested without an LLMClient instance.
  3. Future multi-provider architectures can swap classifiers per provider.
"""

from __future__ import annotations

from src.exceptions import ErrorCode, LLMError


def classify_llm_error(error: Exception) -> LLMError:
    """Map an OpenAI API exception to a structured :class:`LLMError`.

    Uses ``isinstance`` checks against the OpenAI SDK exception hierarchy
    so that each error category gets the right :class:`ErrorCode`,
    user-facing message, and actionable suggestion.

    Args:
        error: A raw exception raised by the OpenAI SDK.

    Returns:
        An :class:`LLMError` with classified ``error_code`` and ``suggestion``.
    """
    from openai import (
        APIConnectionError,
        APITimeoutError,
        AuthenticationError,
        BadRequestError,
        NotFoundError,
        PermissionDeniedError,
        RateLimitError,
    )

    if isinstance(error, AuthenticationError):
        return LLMError(
            message="LLM API authentication failed",
            suggestion="Check your API key in config.json",
            error_code=ErrorCode.LLM_API_KEY_INVALID,
            provider="openai",
        )
    if isinstance(error, PermissionDeniedError):
        return LLMError(
            message="LLM API permission denied",
            suggestion="Verify your API key has access to the requested model",
            error_code=ErrorCode.LLM_API_KEY_INVALID,
            provider="openai",
        )
    if isinstance(error, RateLimitError):
        return LLMError(
            message="LLM API rate limit exceeded",
            suggestion="Wait a moment and try again",
            error_code=ErrorCode.LLM_RATE_LIMITED,
        )
    if isinstance(error, APITimeoutError):
        return LLMError(
            message="LLM API request timed out",
            suggestion="Try again or increase the timeout in config",
            error_code=ErrorCode.LLM_TIMEOUT,
        )
    if isinstance(error, NotFoundError):
        return LLMError(
            message=f"LLM model not found: {error}",
            suggestion="Check the model name in config.json",
            error_code=ErrorCode.LLM_MODEL_UNAVAILABLE,
        )
    if isinstance(error, APIConnectionError):
        return LLMError(
            message="Could not connect to LLM API",
            suggestion="Check your network connection and base_url in config.json",
            error_code=ErrorCode.LLM_CONNECTION_FAILED,
        )
    if isinstance(error, BadRequestError):
        error_msg = str(error).lower()
        if any(
            token in error_msg
            for token in ("context_length", "context length", "max_tokens", "too many tokens")
        ):
            return LLMError(
                message="Conversation exceeds model's context length",
                suggestion="Start a new conversation or reduce message history",
                error_code=ErrorCode.LLM_CONTEXT_LENGTH_EXCEEDED,
            )
        return LLMError(
            message=f"LLM API bad request: {error}",
            suggestion="Check your request parameters",
            error_code=ErrorCode.LLM_INVALID_REQUEST,
        )

    # Generic fallback for any other API error
    return LLMError(
        message=f"LLM API error: {error}",
        suggestion="Check the error details and try again",
    )
