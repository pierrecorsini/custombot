"""
Tests for src/app.py — _classify_main_loop_error() category mapping.

Covers:
- LLM transient codes (TIMEOUT, CONNECTION_FAILED, RATE_LIMITED, CIRCUIT_BREAKER_OPEN)
- LLM permanent codes (API_KEY_INVALID, MODEL_UNAVAILABLE, INVALID_REQUEST, etc.)
- BridgeError and ConnectionError → channel_disconnect
- DatabaseError, DiskSpaceError, OSError → filesystem
- ConfigurationError → configuration
- Generic exceptions → unknown
"""

from __future__ import annotations

import pytest

from src.app import _classify_main_loop_error
from src.exceptions import (
    BridgeError,
    ConfigurationError,
    DatabaseError,
    DiskSpaceError,
    ErrorCode,
    LLMError,
)


# ─────────────────────────────────────────────────────────────────────────────
# LLM Transient
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyLLMTransient:
    """LLM errors with transient error codes map to 'llm_transient'."""

    @pytest.mark.parametrize(
        "error_code",
        [
            ErrorCode.LLM_TIMEOUT,
            ErrorCode.LLM_CONNECTION_FAILED,
            ErrorCode.LLM_RATE_LIMITED,
            ErrorCode.LLM_CIRCUIT_BREAKER_OPEN,
        ],
    )
    def test_transient_error_code(self, error_code: ErrorCode) -> None:
        exc = LLMError(error_code=error_code)
        assert _classify_main_loop_error(exc) == "llm_transient"


# ─────────────────────────────────────────────────────────────────────────────
# LLM Permanent
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyLLMPermanent:
    """LLM errors with non-transient codes map to 'llm_permanent'."""

    @pytest.mark.parametrize(
        "error_code",
        [
            ErrorCode.LLM_API_KEY_INVALID,
            ErrorCode.LLM_MODEL_UNAVAILABLE,
            ErrorCode.LLM_INVALID_REQUEST,
            ErrorCode.LLM_CONTEXT_LENGTH_EXCEEDED,
        ],
    )
    def test_permanent_error_code(self, error_code: ErrorCode) -> None:
        exc = LLMError(error_code=error_code)
        assert _classify_main_loop_error(exc) == "llm_permanent"

    def test_default_error_code_is_permanent(self) -> None:
        """LLMError with no explicit error_code uses UNKNOWN → permanent."""
        exc = LLMError()
        assert _classify_main_loop_error(exc) == "llm_permanent"


# ─────────────────────────────────────────────────────────────────────────────
# Channel Disconnect
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyChannelDisconnect:
    """BridgeError and ConnectionError map to 'channel_disconnect'."""

    def test_bridge_error(self) -> None:
        exc = BridgeError("connection lost")
        assert _classify_main_loop_error(exc) == "channel_disconnect"

    def test_connection_error(self) -> None:
        exc = ConnectionError("refused")
        assert _classify_main_loop_error(exc) == "channel_disconnect"


# ─────────────────────────────────────────────────────────────────────────────
# Filesystem
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyFilesystem:
    """DatabaseError, DiskSpaceError, and OSError map to 'filesystem'."""

    def test_database_error(self) -> None:
        exc = DatabaseError("write failed")
        assert _classify_main_loop_error(exc) == "filesystem"

    def test_disk_space_error(self) -> None:
        exc = DiskSpaceError("disk full")
        assert _classify_main_loop_error(exc) == "filesystem"

    def test_os_error(self) -> None:
        exc = OSError("no space left on device")
        assert _classify_main_loop_error(exc) == "filesystem"

    def test_file_not_found_error_is_os_error_subclass(self) -> None:
        """FileNotFoundError inherits from OSError → filesystem."""
        exc = FileNotFoundError("missing")
        assert _classify_main_loop_error(exc) == "filesystem"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyConfiguration:
    """ConfigurationError maps to 'configuration'."""

    def test_configuration_error(self) -> None:
        exc = ConfigurationError("missing api_key")
        assert _classify_main_loop_error(exc) == "configuration"


# ─────────────────────────────────────────────────────────────────────────────
# Unknown
# ─────────────────────────────────────────────────────────────────────────────


class TestClassifyUnknown:
    """Unrecognized exception types map to 'unknown'."""

    def test_runtime_error(self) -> None:
        exc = RuntimeError("unexpected")
        assert _classify_main_loop_error(exc) == "unknown"

    def test_value_error(self) -> None:
        exc = ValueError("bad value")
        assert _classify_main_loop_error(exc) == "unknown"

    def test_type_error(self) -> None:
        exc = TypeError("wrong type")
        assert _classify_main_loop_error(exc) == "unknown"

    def test_generic_exception(self) -> None:
        exc = Exception("something")
        assert _classify_main_loop_error(exc) == "unknown"
