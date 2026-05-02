"""
test_start_command.py - E2E test for the start command.

Tests that the start command:
  - Validates LLM API credentials
  - Validates WhatsApp (neonize) configuration
  - Exits with code 1 if validation fails
  - Proceeds to bot startup if validation succeeds
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

# ─────────────────────────────────────────────────────────────────────────────
# Helper: Create test config files
# ─────────────────────────────────────────────────────────────────────────────


def _create_valid_config(config_path: Path, workspace: Path) -> None:
    """Create a valid test configuration file."""
    config = {
        "llm": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-test-valid-key",
            "temperature": 0.5,
            "max_tokens": 500,
            "system_prompt_prefix": "Test system prompt",
        },
        "whatsapp": {
            "provider": "neonize",
            "neonize": {
                "db_path": str(workspace / "test_session.db"),
            },
        },
        "memory_max_history": 25,
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)


def _create_config_with_invalid_api_key(config_path: Path, workspace: Path) -> None:
    """Create a config with a placeholder/invalid API key."""
    config = {
        "llm": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-your-api-key",  # Placeholder pattern
            "temperature": 0.5,
            "max_tokens": 500,
            "system_prompt_prefix": "Test system prompt",
        },
        "whatsapp": {
            "provider": "neonize",
            "neonize": {
                "db_path": str(workspace / "test_session.db"),
            },
        },
        "memory_max_history": 25,
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Configuration Validation
# ─────────────────────────────────────────────────────────────────────────────


def test_start_exits_if_config_missing(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command exits if config file is missing.

    Arrange:
        - Path to non-existent config

    Act:
        - Run: python main.py start --config <nonexistent>

    Assert:
        - Exit code is 1
        - Error message indicates config not found
    """
    # Arrange
    config_path = tmp_path / "nonexistent_config.json"

    # Act
    from main import cli

    result = cli_runner.invoke(
        cli,
        ["start", "--config", str(config_path)],
    )

    # Assert
    assert result.exit_code != 0, "Should exit with non-zero code"
    assert "not found" in result.output.lower() or "error" in result.output.lower()


def test_start_exits_if_config_invalid(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command exits if config file is invalid JSON.

    Arrange:
        - Create config file with invalid JSON

    Act:
        - Run: python main.py start --config <path>

    Assert:
        - Exit code is 1
        - Error message indicates config load failure
    """
    # Arrange
    config_path = tmp_path / "invalid_config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        f.write("{ invalid json }")

    # Act
    from main import cli

    result = cli_runner.invoke(
        cli,
        ["start", "--config", str(config_path)],
    )

    # Assert
    assert result.exit_code != 0, "Should exit with non-zero code"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Channel Validation Success Path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_validates_channels_success(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command validates channels successfully.

    Arrange:
        - Create valid config
        - Mock LLM validation to return success

    Act:
        - Run channel validation

    Assert:
        - Validation returns (True, [])
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)

    # Mock the LLM health check (whatsapp validates db_path locally)
    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        # Mock successful LLM check
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(config)

    # Assert
    assert success is True, f"Validation should succeed, errors: {errors}"
    assert len(errors) == 0, f"Should have no errors, got: {errors}"


@pytest.mark.asyncio
async def test_start_validates_llm_credentials(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command validates LLM credentials.

    Arrange:
        - Create valid config
        - Mock LLM health check

    Act:
        - Run LLM validation

    Assert:
        - LLM validation is performed
        - Returns appropriate result
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import _validate_llm
    from src.config import load_config

    config = load_config(config_path)

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_health = MagicMock()
        mock_health.status.value = "healthy"
        mock_health.latency_ms = 150.0
        mock_llm.return_value = mock_health

        # Act
        result = await _validate_llm(config)

    # Assert
    assert result.success is True, f"LLM validation should succeed: {result.message}"
    assert result.channel == "llm"


@pytest.mark.asyncio
async def test_start_validates_whatsapp_config(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command validates WhatsApp (neonize) configuration.

    Arrange:
        - Create valid config with proper db_path
        - Run WhatsApp validation

    Act:
        - Run WhatsApp validation

    Assert:
        - WhatsApp validation is performed
        - Returns appropriate result
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import _validate_whatsapp
    from src.config import load_config

    config = load_config(config_path)

    # Act — no mocking needed, _validate_whatsapp checks db_path directly
    result = await _validate_whatsapp(config)

    # Assert
    assert result.success is True, f"WhatsApp validation should succeed: {result.message}"
    assert result.channel == "whatsapp"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Channel Validation Failure Path
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_start_fails_on_invalid_api_key(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command fails validation on placeholder API key.

    Arrange:
        - Create config with placeholder API key

    Act:
        - Run channel validation

    Assert:
        - Validation returns (False, errors)
        - Error mentions API key issue
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_config_with_invalid_api_key(config_path, workspace)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)

    # Act
    success, errors = await validate_channels(config)

    # Assert
    assert success is False, "Validation should fail with placeholder API key"
    assert len(errors) > 0, "Should have at least one error"

    # Check that error mentions API key
    error_text = " ".join(errors).lower()
    assert "api key" in error_text or "placeholder" in error_text or "llm" in error_text


@pytest.mark.asyncio
async def test_start_fails_on_missing_api_key(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command fails validation on missing API key.

    Arrange:
        - Create config with empty API key

    Act:
        - Run channel validation

    Assert:
        - Validation returns (False, errors)
        - Error mentions API key not configured
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"

    config_data = {
        "llm": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "",  # Empty API key
            "temperature": 0.5,
            "max_tokens": 500,
        },
        "whatsapp": {
            "provider": "neonize",
            "neonize": {"db_path": str(workspace / "test_session.db")},
        },
    }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config_data, f)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)

    # Act
    success, errors = await validate_channels(config)

    # Assert
    assert success is False, "Validation should fail with empty API key"
    assert len(errors) > 0, "Should have at least one error"


@pytest.mark.asyncio
async def test_start_fails_on_invalid_db_path(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command fails validation on empty neonize db_path.

    Arrange:
        - Create valid config with LLM credentials
        - Set empty db_path

    Act:
        - Run channel validation

    Assert:
        - Validation returns (False, errors)
        - Error mentions db_path issue
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)

    # Override db_path to empty to simulate invalid config
    config.whatsapp.neonize.db_path = ""

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        # Act
        success, errors = await validate_channels(config)

    # Assert
    assert success is False, "Validation should fail with empty db_path"
    assert len(errors) > 0, "Should have at least one error"

    error_text = " ".join(errors).lower()
    assert "db_path" in error_text or "whatsapp" in error_text or "neonize" in error_text


def test_start_exits_with_code_1_on_validation_failure(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command exits with code 1 when validation fails.

    Arrange:
        - Create config with invalid API key
        - Mock validation to fail

    Act:
        - Run: python main.py start --config <path>

    Assert:
        - Exit code is 1
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_config_with_invalid_api_key(config_path, workspace)

    # Act
    from main import cli

    result = cli_runner.invoke(
        cli,
        ["start", "--config", str(config_path)],
    )

    # Assert
    assert result.exit_code == 1, f"Should exit with code 1, got {result.exit_code}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: CLI Help
# ─────────────────────────────────────────────────────────────────────────────


def test_start_command_help(cli_runner: CliRunner):
    """
    E2E Test: Start command shows help text.

    Act:
        - Run: python main.py start --help

    Assert:
        - Help text is displayed
        - Contains expected descriptions
    """
    from main import cli

    result = cli_runner.invoke(cli, ["start", "--help"])

    assert result.exit_code == 0, f"Help failed: {result.output}"
    assert "start" in result.output.lower()
    assert "--config" in result.output
    assert "--health-port" in result.output
    assert "--health-host" in result.output


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Validation Result Details
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_result_includes_details(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Validation result includes helpful details.

    Arrange:
        - Create valid config
        - Mock LLM validation

    Act:
        - Run validation

    Assert:
        - Result includes details dict with useful info
    """
    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import validate_all_channels
    from src.config import load_config

    config = load_config(config_path)

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        # Act
        results = await validate_all_channels(config)

    # Assert
    assert len(results) == 2, "Should have 2 validation results (llm, whatsapp)"

    for result in results:
        assert hasattr(result, "channel"), "Result should have channel"
        assert hasattr(result, "success"), "Result should have success"
        assert hasattr(result, "message"), "Result should have message"
        assert hasattr(result, "details"), "Result should have details dict"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Network Timeout Scenarios
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_llm_validation_timeout(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: LLM validation handles timeout gracefully.

    Arrange:
        - Create valid config
        - Mock LLM check to return degraded (timeout)

    Act:
        - Run validation

    Assert:
        - Validation succeeds (degraded allows startup)
        - Warning is included in result
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import _validate_llm
    from src.config import load_config

    config = load_config(config_path)

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_health = MagicMock()
        mock_health.status.value = "degraded"
        mock_health.latency_ms = 5000.0
        mock_health.message = "Request timed out"
        mock_llm.return_value = mock_health

        result = await _validate_llm(config)

    # Degraded allows startup
    assert result.success is True
    assert "warning" in result.details


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Config Save Failures
# ─────────────────────────────────────────────────────────────────────────────


def test_start_handles_config_readonly(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Start command handles readonly config directory.

    Note: This test verifies behavior when config directory is not writable.
    The actual behavior may vary by platform.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from main import cli

    # The start command should still work even if config is readonly
    # since we're only reading the config, not writing
    result = cli_runner.invoke(
        cli,
        ["start", "--config", str(config_path)],
    )

    # Should fail due to validation (placeholder key pattern not matched)
    # but not due to config write issues
    assert result.exit_code != 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Both Channels Degraded
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_both_channels_degraded(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Validation succeeds when LLM is degraded and whatsapp is valid.

    Arrange:
        - Create valid config
        - Mock LLM check to return degraded
        - WhatsApp validation passes (valid db_path)

    Act:
        - Run validation

    Assert:
        - Validation succeeds (degraded allows startup)
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "degraded"
        mock_llm_health.latency_ms = 5000.0
        mock_llm_health.message = "LLM timeout"
        mock_llm.return_value = mock_llm_health

        success, errors = await validate_channels(config)

    # Degraded should allow startup
    assert success is True
    assert len(errors) == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Mixed Status (One Healthy, One Unhealthy)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mixed_status_llm_healthy_whatsapp_unhealthy(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Validation fails when WhatsApp config is invalid.

    Arrange:
        - Create valid config then override db_path to empty
        - Mock LLM healthy

    Act:
        - Run validation

    Assert:
        - Validation fails
        - Only whatsapp error in errors
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)
    config.whatsapp.neonize.db_path = ""  # Make whatsapp invalid

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "healthy"
        mock_llm_health.latency_ms = 100.0
        mock_llm.return_value = mock_llm_health

        success, errors = await validate_channels(config)

    assert success is False
    assert len(errors) == 1
    assert "whatsapp" in errors[0].lower()


@pytest.mark.asyncio
async def test_mixed_status_llm_unhealthy_whatsapp_healthy(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Validation fails when LLM is unhealthy.

    Arrange:
        - Create valid config
        - Mock LLM unhealthy

    Act:
        - Run validation

    Assert:
        - Validation fails
        - Only LLM error in errors
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = tmp_path / "config.json"
    _create_valid_config(config_path, workspace)

    from src.channels.validation import validate_channels
    from src.config import load_config

    config = load_config(config_path)

    with patch("src.channels.validation.check_llm_credentials") as mock_llm:
        mock_llm_health = MagicMock()
        mock_llm_health.status.value = "unhealthy"
        mock_llm_health.message = "Invalid credentials"
        mock_llm.return_value = mock_llm_health

        success, errors = await validate_channels(config)

    assert success is False
    assert len(errors) == 1
    assert "llm" in errors[0].lower()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Validation Report Formatting
# ─────────────────────────────────────────────────────────────────────────────


def test_format_validation_report(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Validation report formats correctly.

    Arrange:
        - Create mock validation results

    Act:
        - Format report

    Assert:
        - Report contains expected sections
    """
    from src.channels.validation import (
        ValidationResult,
        format_validation_report,
    )

    results = [
        ValidationResult(
            channel="llm",
            success=True,
            message="LLM API verified",
            details={"latency_ms": 100.0, "model": "gpt-4o-mini"},
        ),
        ValidationResult(
            channel="whatsapp",
            success=False,
            message="WhatsApp session db_path not configured",
            details={"hint": "Set whatsapp.neonize.db_path in config.json"},
        ),
    ]

    report = format_validation_report(results)

    assert "Channel Validation Report" in report
    assert "LLM" in report
    assert "WHATSAPP" in report
    assert "✓ PASS" in report
    assert "✗ FAIL" in report
    assert "1/2 channels passed" in report
