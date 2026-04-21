"""
test_channel_validation.py - Unit tests for channel_validation module.

Tests all validation functions including:
- validate_channels() success and failure cases
- _validate_llm() with various credential states
- _validate_whatsapp() with various neonize config states
- Edge cases: empty config, invalid API key, invalid db_path, non-writable paths
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.validation import (
    ValidationResult,
    _validate_llm,
    _validate_whatsapp,
    format_validation_report,
    validate_all_channels,
    validate_channels,
)
from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig
from src.health import HealthStatus

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def valid_config(temp_workspace: Path) -> Config:
    """Create a valid configuration for testing."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-valid-test-key-12345",
            temperature=0.7,
            max_tokens=500,
            system_prompt_prefix="Test prompt",
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(
                db_path=str(temp_workspace / "test_session.db"),
            ),
        ),
    )


@pytest.fixture
def config_with_empty_api_key(temp_workspace: Path) -> Config:
    """Create a config with an empty API key."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="",  # Empty API key
            temperature=0.7,
            max_tokens=500,
            system_prompt_prefix="Test prompt",
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(
                db_path=str(temp_workspace / "test_session.db"),
            ),
        ),
    )


@pytest.fixture
def config_with_placeholder_api_key(temp_workspace: Path) -> Config:
    """Create a config with a placeholder API key."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-your-api-key",  # Placeholder pattern
            temperature=0.7,
            max_tokens=500,
            system_prompt_prefix="Test prompt",
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(
                db_path=str(temp_workspace / "test_session.db"),
            ),
        ),
    )


@pytest.fixture
def config_with_empty_db_path(temp_workspace: Path) -> Config:
    """Create a config with empty neonize db_path."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-valid-test-key-12345",
            temperature=0.7,
            max_tokens=500,
            system_prompt_prefix="Test prompt",
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(
                db_path="",  # Empty db_path
            ),
        ),
    )


@pytest.fixture
def config_with_invalid_provider(temp_workspace: Path) -> Config:
    """Create a config with unsupported WhatsApp provider."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-valid-test-key-12345",
            temperature=0.7,
            max_tokens=500,
            system_prompt_prefix="Test prompt",
        ),
        whatsapp=WhatsAppConfig(
            provider="baileys",  # Unsupported provider
            neonize=NeonizeConfig(
                db_path=str(temp_workspace / "test_session.db"),
            ),
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ValidationResult dataclass
# ─────────────────────────────────────────────────────────────────────────────


def test_validation_result_to_dict():
    """Test ValidationResult serialization to dictionary."""
    result = ValidationResult(
        channel="llm",
        success=True,
        message="LLM API credentials verified",
        details={"model": "gpt-4o-mini", "latency_ms": 100.0},
    )

    data = result.to_dict()

    assert data["channel"] == "llm"
    assert data["success"] is True
    assert data["message"] == "LLM API credentials verified"
    assert data["details"]["model"] == "gpt-4o-mini"
    assert data["details"]["latency_ms"] == 100.0


def test_validation_result_default_details():
    """Test ValidationResult with default empty details."""
    result = ValidationResult(
        channel="whatsapp",
        success=False,
        message="Neonize not configured",
    )

    assert result.details == {}
    data = result.to_dict()
    assert data["details"] == {}


# ─────────────────────────────────────────────────────────────────────────────
# Tests: validate_channels() success case
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_success(valid_config: Config):
    """
    Unit Test: validate_channels returns success when all channels are healthy.

    Arrange:
        - Valid config with proper API key and neonize db_path
        - Mock LLM health check to return healthy

    Act:
        - Call validate_channels()

    Assert:
        - Returns (True, [])
    """
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        # Mock successful LLM check
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(valid_config)

    # Assert
    assert success is True, f"Expected success, got errors: {errors}"
    assert len(errors) == 0, f"Expected no errors, got: {errors}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: validate_channels() LLM failure case
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_llm_failure(valid_config: Config):
    """
    Unit Test: validate_channels returns failure when LLM credentials are invalid.

    Arrange:
        - Valid config
        - Mock LLM check to return unhealthy

    Act:
        - Call validate_channels()

    Assert:
        - Returns (False, [error])
        - Error mentions LLM
    """
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        # Mock failed LLM check
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "unhealthy"
        mock_llm_health.message = "Invalid API key"
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(valid_config)

    # Assert
    assert success is False, "Expected failure with invalid LLM credentials"
    assert len(errors) == 1, f"Expected 1 error, got: {errors}"
    assert "LLM" in errors[0] or "llm" in errors[0].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: validate_channels() WhatsApp (neonize) failure case
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_whatsapp_failure(config_with_empty_db_path: Config):
    """
    Unit Test: validate_channels returns failure when neonize db_path is empty.

    Arrange:
        - Config with empty db_path
        - Mock LLM check to return healthy

    Act:
        - Call validate_channels()

    Assert:
        - Returns (False, [error])
        - Error mentions whatsapp/neonize
    """
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        # Mock successful LLM check
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(config_with_empty_db_path)

    # Assert
    assert success is False, "Expected failure with empty db_path"
    assert len(errors) == 1, f"Expected 1 error, got: {errors}"
    error_text = errors[0].lower()
    assert "whatsapp" in error_text or "neonize" in error_text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: validate_channels() both failures case
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_both_failures(config_with_empty_db_path: Config):
    """
    Unit Test: validate_channels returns both errors when both channels fail.

    Arrange:
        - Config with empty db_path (whatsapp fails)
        - Mock LLM check to return unhealthy

    Act:
        - Call validate_channels()

    Assert:
        - Returns (False, [error1, error2])
        - Both errors present
    """
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        # Mock failed LLM check
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "unhealthy"
        mock_llm_health.message = "Invalid credentials"
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(config_with_empty_db_path)

    # Assert
    assert success is False, "Expected failure with both channels unhealthy"
    assert len(errors) == 2, f"Expected 2 errors, got: {errors}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Edge case - missing API key
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_missing_api_key(config_with_empty_api_key: Config):
    """
    Unit Test: validate_channels fails when API key is empty.

    Arrange:
        - Config with empty API key

    Act:
        - Call validate_channels()

    Assert:
        - Returns (False, [error])
        - Error mentions API key not configured
    """
    # Act
    success, errors = await validate_channels(config_with_empty_api_key)

    # Assert
    assert success is False, "Expected failure with empty API key"
    assert len(errors) >= 1, "Expected at least one error"
    error_text = " ".join(errors).lower()
    assert "api key" in error_text or "not configured" in error_text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Edge case - placeholder API key
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_placeholder_api_key(
    config_with_placeholder_api_key: Config,
):
    """
    Unit Test: validate_channels fails when API key is a placeholder.

    Arrange:
        - Config with placeholder API key (sk-your-api-key)

    Act:
        - Call validate_channels()

    Assert:
        - Returns (False, [error])
        - Error mentions placeholder
    """
    # Act
    success, errors = await validate_channels(config_with_placeholder_api_key)

    # Assert
    assert success is False, "Expected failure with placeholder API key"
    assert len(errors) >= 1, "Expected at least one error"
    error_text = " ".join(errors).lower()
    assert "placeholder" in error_text or "api key" in error_text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Edge case - invalid db_path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_channels_empty_db_path(
    config_with_empty_db_path: Config,
):
    """
    Unit Test: validate_channels fails when neonize db_path is empty.

    Arrange:
        - Config with empty db_path

    Act:
        - Call validate_channels()

    Assert:
        - Returns (False, [error])
        - Error mentions db_path not configured
    """
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        # Mock successful LLM check
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(config_with_empty_db_path)

    # Assert
    assert success is False, "Expected failure with empty db_path"
    assert len(errors) >= 1, "Expected at least one error"
    error_text = " ".join(errors).lower()
    assert "db_path" in error_text or "neonize" in error_text or "whatsapp" in error_text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _validate_llm() detailed tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_llm_success(valid_config: Config):
    """Test _validate_llm with healthy response."""
    with patch("src.channels.validation.check_llm_credentials") as mock_check:
        mock_health = MagicMock()
        mock_health.status.value = "healthy"
        mock_health.latency_ms = 150.0
        mock_check.return_value = mock_health

        result = await _validate_llm(valid_config)

    assert result.success is True
    assert result.channel == "llm"
    assert "latency_ms" in result.details
    assert result.details["latency_ms"] == 150.0


@pytest.mark.asyncio
async def test_validate_llm_degraded(valid_config: Config):
    """Test _validate_llm with degraded (timeout) response."""
    with patch("src.channels.validation.check_llm_credentials") as mock_check:
        mock_health = MagicMock()
        mock_health.status.value = "degraded"
        mock_health.latency_ms = 5000.0
        mock_health.message = "Request timed out"
        mock_check.return_value = mock_health

        result = await _validate_llm(valid_config)

    # Degraded still counts as success (allow startup)
    assert result.success is True
    assert "warning" in result.details


@pytest.mark.asyncio
async def test_validate_llm_unhealthy(valid_config: Config):
    """Test _validate_llm with unhealthy (invalid credentials) response."""
    with patch("src.channels.validation.check_llm_credentials") as mock_check:
        mock_health = MagicMock()
        mock_health.status.value = "unhealthy"
        mock_health.message = "Invalid API key"
        mock_check.return_value = mock_health

        result = await _validate_llm(valid_config)

    assert result.success is False
    assert "hint" in result.details


@pytest.mark.asyncio
async def test_validate_llm_empty_api_key(config_with_empty_api_key: Config):
    """Test _validate_llm with empty API key - no API call made."""
    result = await _validate_llm(config_with_empty_api_key)

    assert result.success is False
    assert "not configured" in result.message.lower()
    assert "hint" in result.details


@pytest.mark.asyncio
async def test_validate_llm_placeholder_patterns(valid_config: Config):
    """Test _validate_llm with various placeholder patterns."""
    placeholder_patterns = [
        "sk-your-api-key",
        "YOUR_API_KEY",
        "your-api-key-here",
        "sk-xxx-test",
    ]

    for placeholder in placeholder_patterns:
        config = Config(
            llm=LLMConfig(
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key=placeholder,
                temperature=0.7,
                max_tokens=500,
                system_prompt_prefix="Test",
            ),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path="workspace/test_session.db"),
            ),
        )

        result = await _validate_llm(config)

        assert result.success is False, f"Expected failure for placeholder: {placeholder}"
        assert "placeholder" in result.message.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _validate_whatsapp() detailed tests
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_whatsapp_success(valid_config: Config):
    """Test _validate_whatsapp with valid neonize config."""
    result = await _validate_whatsapp(valid_config)

    assert result.success is True
    assert result.channel == "whatsapp"
    assert "db_path" in result.details


@pytest.mark.asyncio
async def test_validate_whatsapp_empty_db_path(config_with_empty_db_path: Config):
    """Test _validate_whatsapp with empty db_path - fails immediately."""
    result = await _validate_whatsapp(config_with_empty_db_path)

    assert result.success is False
    assert "not configured" in result.message.lower()
    assert "hint" in result.details


@pytest.mark.asyncio
async def test_validate_whatsapp_unsupported_provider(
    config_with_invalid_provider: Config,
):
    """Test _validate_whatsapp with unsupported provider."""
    result = await _validate_whatsapp(config_with_invalid_provider)

    assert result.success is False
    assert "unsupported" in result.message.lower() or "provider" in result.message.lower()
    assert "hint" in result.details


@pytest.mark.asyncio
async def test_validate_whatsapp_non_writable_path(valid_config: Config):
    """Test _validate_whatsapp with non-writable db_path directory."""
    with patch("src.channels.validation.Path") as mock_path_cls:
        mock_path = MagicMock()
        mock_path.parent.mkdir.side_effect = OSError("Permission denied")
        mock_path_cls.return_value = mock_path

        result = await _validate_whatsapp(valid_config)

    assert result.success is False
    assert "not writable" in result.message.lower() or "permission" in result.message.lower()
    assert "hint" in result.details


# ─────────────────────────────────────────────────────────────────────────────
# Tests: validate_all_channels()
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_all_channels_returns_two_results(valid_config: Config):
    """Test validate_all_channels returns results for both channels."""
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        results = await validate_all_channels(valid_config)

    assert len(results) == 2, "Should return 2 validation results"
    channels = [r.channel for r in results]
    assert "llm" in channels
    assert "whatsapp" in channels


@pytest.mark.asyncio
async def test_validate_all_channels_result_structure(valid_config: Config):
    """Test that each ValidationResult has proper structure."""
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        results = await validate_all_channels(valid_config)

    for result in results:
        assert isinstance(result, ValidationResult)
        assert hasattr(result, "channel")
        assert hasattr(result, "success")
        assert hasattr(result, "message")
        assert hasattr(result, "details")
        assert callable(result.to_dict)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: format_validation_report()
# ─────────────────────────────────────────────────────────────────────────────


def test_format_validation_report_all_passed():
    """Test report formatting when all channels pass."""
    results = [
        ValidationResult(
            channel="llm",
            success=True,
            message="LLM API credentials verified",
            details={"latency_ms": 100.0, "model": "gpt-4o-mini"},
        ),
        ValidationResult(
            channel="whatsapp",
            success=True,
            message="WhatsApp (neonize) configuration valid",
            details={"db_path": "workspace/test_session.db"},
        ),
    ]

    report = format_validation_report(results)

    assert "Channel Validation Report" in report
    assert "LLM" in report
    assert "WHATSAPP" in report
    assert "✓ PASS" in report
    assert "2/2 channels passed" in report


def test_format_validation_report_with_failures():
    """Test report formatting when some channels fail."""
    results = [
        ValidationResult(
            channel="llm",
            success=True,
            message="LLM API credentials verified",
            details={"latency_ms": 100.0},
        ),
        ValidationResult(
            channel="whatsapp",
            success=False,
            message="WhatsApp session db_path not writable",
            details={"hint": "Ensure the directory for db_path exists and is writable"},
        ),
    ]

    report = format_validation_report(results)

    assert "✓ PASS" in report
    assert "✗ FAIL" in report
    assert "1/2 channels passed" in report
    assert "Hint:" in report


def test_format_validation_report_with_warnings():
    """Test report formatting includes warnings."""
    results = [
        ValidationResult(
            channel="llm",
            success=True,
            message="LLM check timed out",
            details={"warning": "Timeout after 5s"},
        ),
    ]

    report = format_validation_report(results)

    assert "Warning:" in report


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Network timeout scenarios
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validate_llm_timeout(valid_config: Config):
    """Test LLM validation handles timeout gracefully."""
    with patch("src.channels.validation.check_llm_credentials") as mock_check:
        mock_health = MagicMock()
        mock_health.status.value = "degraded"
        mock_health.latency_ms = 5000.0
        mock_health.message = "Request timed out"
        mock_check.return_value = mock_health

        result = await _validate_llm(valid_config)

    # Timeout should still allow startup (degraded)
    assert result.success is True
    assert "timeout" in result.message.lower() or "timed out" in result.message.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Error message quality
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_messages_include_hints(valid_config: Config):
    """Test that failed validations include helpful hints."""
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "unhealthy"
        mock_llm_health.message = "Invalid credentials"
        mock_llm.return_value = mock_llm_health

        results = await validate_all_channels(config_with_empty_db_path_fixture(valid_config))

    for result in results:
        if not result.success:
            assert "hint" in result.details, f"Missing hint for {result.channel}"
            assert len(result.details["hint"]) > 0


def config_with_empty_db_path_fixture(valid_config: Config) -> Config:
    """Helper to create a config with empty db_path from a valid config."""
    return Config(
        llm=valid_config.llm,
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=""),
        ),
    )


@pytest.mark.asyncio
async def test_llm_error_includes_base_url_and_model(valid_config: Config):
    """Test LLM validation error includes configuration details."""
    with patch("src.channels.validation.check_llm_credentials") as mock_check:
        mock_health = MagicMock()
        mock_health.status.value = "unhealthy"
        mock_health.message = "Invalid API key"
        mock_check.return_value = mock_health

        result = await _validate_llm(valid_config)

    assert "base_url" in result.details
    assert "model" in result.details
    assert result.details["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_whatsapp_error_includes_hint():
    """Test WhatsApp validation error includes helpful hint."""
    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=""),
        ),
    )

    result = await _validate_whatsapp(config)

    assert result.success is False
    assert "hint" in result.details


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Additional edge cases
# ─────────────────────────────────────────────────────────────────────────────


def test_format_validation_report_empty():
    """Test report formatting with no results."""
    report = format_validation_report([])
    assert "0/0 channels passed" in report


def test_format_validation_report_latency():
    """Test report includes latency when present."""
    results = [
        ValidationResult(
            channel="llm",
            success=True,
            message="OK",
            details={"latency_ms": 42.5},
        ),
    ]
    report = format_validation_report(results)
    assert "42.50ms" in report


def test_validation_result_to_dict_roundtrip():
    """Test ValidationResult to_dict preserves all fields."""
    original = ValidationResult(
        channel="llm",
        success=False,
        message="Error",
        details={"hint": "Check key", "model": "gpt-4"},
    )
    data = original.to_dict()
    assert data == {
        "channel": "llm",
        "success": False,
        "message": "Error",
        "details": {"hint": "Check key", "model": "gpt-4"},
    }


@pytest.mark.asyncio
async def test_validate_channels_includes_hint_in_error_message():
    """Test that validate_channels appends hints to error messages."""
    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=""),
        ),
    )
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "unhealthy"
        mock_llm_health.message = "Bad key"
        mock_llm.return_value = mock_llm_health

        success, errors = await validate_channels(config)

    assert success is False
    # At least one error should include a hint
    has_hint = any("Hint:" in e for e in errors)
    assert has_hint, f"Expected at least one error with Hint: {errors}"


@pytest.mark.asyncio
async def test_validate_llm_all_placeholder_patterns():
    """Test every placeholder pattern is detected."""
    patterns = ["sk-your-api-key", "your_api_key", "YOUR-API-KEY", "xxx"]
    for p in patterns:
        config = Config(
            llm=LLMConfig(api_key=p, model="m", base_url="http://x"),
            whatsapp=WhatsAppConfig(
                provider="neonize",
                neonize=NeonizeConfig(db_path="workspace/test.db"),
            ),
        )
        result = await _validate_llm(config)
        assert result.success is False, f"Pattern {p!r} should be detected as placeholder"


@pytest.mark.asyncio
async def test_validate_whatsapp_creates_parent_dir(tmp_path):
    """Test that validate_whatsapp creates parent directory if needed."""
    db_path = tmp_path / "new_subdir" / "session.db"
    config = Config(
        llm=LLMConfig(api_key="sk-test", model="m", base_url="http://x"),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(db_path=str(db_path)),
        ),
    )
    result = await _validate_whatsapp(config)
    assert result.success is True
    assert (tmp_path / "new_subdir").is_dir()
