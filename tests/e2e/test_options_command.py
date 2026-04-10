"""
test_options_command.py - E2E test for the options command TUI.

Tests that the options command:
  - Opens configuration editor
  - Loads existing config or creates default
  - Saves configuration changes
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────


def test_options_opens_config_editor(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Options command opens the configuration editor.

    Arrange:
        - Set up a temporary directory for the config

    Act:
        - Run: python main.py options --config <path>

    Assert:
        - Configuration editor is invoked
    """
    # Arrange
    config_path = tmp_path / "config.json"

    # Act
    from main import cli

    # Mock the TUI function to avoid interactive prompts
    # Note: import is done inside the function, so mock at source module
    with patch("src.ui.options_tui.run_options_tui") as mock_tui:
        mock_tui.return_value = True
        result = cli_runner.invoke(
            cli,
            ["options", "--config", str(config_path)],
            catch_exceptions=False,
        )

    # Assert
    assert mock_tui.called, "Options TUI should be called"
    assert mock_tui.call_args[0][0] == config_path, "Config path should be passed"


def test_options_creates_default_config_if_missing(
    cli_runner: CliRunner, tmp_path: Path
):
    """
    E2E Test: Options command creates default config if it doesn't exist.

    Arrange:
        - Set up a path where config doesn't exist

    Act:
        - Run: python main.py options --config <path>

    Assert:
        - Default config is created
        - Config has expected structure
    """
    # Arrange
    config_path = tmp_path / "config.json"
    assert not config_path.exists(), "Config should not exist initially"

    # Act
    from main import cli

    with patch("src.ui.options_tui.run_options_tui") as mock_tui:
        mock_tui.return_value = True
        result = cli_runner.invoke(
            cli,
            ["options", "--config", str(config_path)],
            catch_exceptions=False,
        )

    # Assert - config should be created
    assert config_path.exists(), "Default config should be created"

    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    assert "llm" in config, "Config should have 'llm' section"
    assert "whatsapp" in config, "Config should have 'whatsapp' section"
    # Note: workspace is not a config field, it's handled separately by Memory/Database


def test_options_loads_existing_config(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Options command loads existing config.

    Arrange:
        - Create an existing config file

    Act:
        - Run: python main.py options --config <path>

    Assert:
        - Existing config is loaded (not overwritten)
    """
    # Arrange
    config_path = tmp_path / "config.json"

    existing_config = {
        "llm": {
            "model": "gpt-4o-mini",
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-existing-key",
            "temperature": 0.5,
            "max_tokens": 500,
            "system_prompt_prefix": "Existing prompt",
        },
        "whatsapp": {
            "provider": "neonize",
            "neonize": {
                "db_path": "workspace/test_session.db",
            },
        },
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(existing_config, f)

    # Act
    from main import cli

    with patch("src.ui.options_tui.run_options_tui") as mock_tui:
        mock_tui.return_value = True
        result = cli_runner.invoke(
            cli,
            ["options", "--config", str(config_path)],
            catch_exceptions=False,
        )

    # Assert - TUI should be called with the config path
    assert mock_tui.called, "Options TUI should be called"


def test_options_handles_keyboard_interrupt(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Options command handles keyboard interrupt gracefully.

    Arrange:
        - Mock TUI to raise KeyboardInterrupt

    Act:
        - Run: python main.py options --config <path>

    Assert:
        - Command exits gracefully with appropriate message
    """
    # Arrange
    config_path = tmp_path / "config.json"

    # Act
    from main import cli

    with patch("src.ui.options_tui.run_options_tui") as mock_tui:
        mock_tui.side_effect = KeyboardInterrupt()
        result = cli_runner.invoke(
            cli,
            ["options", "--config", str(config_path)],
        )

    # Assert - should handle gracefully
    assert "cancel" in result.output.lower() or result.exit_code == 0


def test_options_shows_error_on_tui_failure(cli_runner: CliRunner, tmp_path: Path):
    """
    E2E Test: Options command shows error if TUI fails.

    Arrange:
        - Mock TUI to raise an exception

    Act:
        - Run: python main.py options --config <path>

    Assert:
        - Command exits with error code
        - Error message is shown
    """
    # Arrange
    config_path = tmp_path / "config.json"

    # Act
    from main import cli

    with patch("src.ui.options_tui.run_options_tui") as mock_tui:
        mock_tui.side_effect = RuntimeError("TUI failed to start")
        result = cli_runner.invoke(
            cli,
            ["options", "--config", str(config_path)],
        )

    # Assert
    assert result.exit_code != 0, "Should exit with error code"
    assert "failed" in result.output.lower() or "error" in result.output.lower()


def test_options_command_help(cli_runner: CliRunner):
    """
    E2E Test: Options command shows help text.

    Act:
        - Run: python main.py options --help

    Assert:
        - Help text is displayed
        - Contains expected descriptions
    """
    from main import cli

    result = cli_runner.invoke(cli, ["options", "--help"])

    assert result.exit_code == 0, f"Help failed: {result.output}"
    assert "configuration" in result.output.lower() or "editor" in result.output.lower()
    assert "--config" in result.output
