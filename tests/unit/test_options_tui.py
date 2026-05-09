"""
test_options_tui.py - Unit tests for options_tui module.

Tests the TUI functions for configuration editing with mocked questionary.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig, save_config
from src.ui.options_tui import (
    _edit_general_settings,
    _edit_llm_settings,
    _edit_whatsapp_settings,
    _show_main_menu,
    run_options_tui,
)
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def test_config(temp_workspace: Path) -> Config:
    """Create a test configuration."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-key",
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
def config_file(temp_config_path: Path, test_config: Config) -> Path:
    """Create a test config file."""
    save_config(test_config, temp_config_path)
    return temp_config_path


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _show_main_menu
# ─────────────────────────────────────────────────────────────────────────────


def test_show_main_menu_returns_selection():
    """Test that _show_main_menu returns the selected value."""
    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "llm"

        result = _show_main_menu()

        assert result == "llm"
        mock_select.assert_called_once()


def test_show_main_menu_cancel_returns_none():
    """Test that _show_main_menu returns None when cancelled."""
    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = None

        result = _show_main_menu()

        assert result is None


def test_show_main_menu_has_all_choices():
    """Test that main menu includes all expected choices."""
    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "save"

        _show_main_menu()

        # Get the choices argument
        call_args = mock_select.call_args
        choices = call_args[1]["choices"]

        # Extract choice values
        values = [c.value if hasattr(c, "value") else c for c in choices]

        assert "llm" in values
        assert "whatsapp" in values
        assert "general" in values
        assert "save" in values
        assert "cancel" in values


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _edit_llm_settings
# ─────────────────────────────────────────────────────────────────────────────


def test_edit_llm_settings_applies_changes(test_config: Config):
    """Test that _edit_llm_settings applies input changes to config."""
    with (
        patch("src.ui.options_tui.questionary.select") as mock_select,
        patch("src.ui.options_tui.questionary.password") as mock_password,
        patch("src.ui.options_tui.questionary.text") as mock_text,
    ):
        # New flow: select field, edit it, select another field, edit it, select "__done__" to save changes
        # Only 4 fields now: api_key, model, base_url, temperature
        mock_select.return_value.ask.side_effect = [
            "api_key",  # Select API Key field
            "model",  # Select Model field
            "base_url",  # Select Base URL field
            "temperature",  # Select Temperature field
            "__done__",  # Done - save changes
        ]

        # Input values for each field
        mock_password.return_value.ask.return_value = "sk-new-api-key"
        mock_text.return_value.ask.side_effect = [
            "gpt-4o",  # model
            "https://api.new.com/v1",  # base_url
            "0.5",  # temperature
        ]

        result = _edit_llm_settings(test_config)

        assert result is True
        assert test_config.llm.api_key == "sk-new-api-key"
        assert test_config.llm.model == "gpt-4o"
        assert test_config.llm.base_url == "https://api.new.com/v1"
        assert test_config.llm.temperature == 0.5
        # max_tokens is now optional, not set via UI


def test_edit_llm_settings_cancel_returns_false(test_config: Config):
    """Test that _edit_llm_settings returns False when cancelled."""
    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "__cancel__"  # Cancelled

        result = _edit_llm_settings(test_config)

        assert result is False


def test_edit_llm_settings_invalid_max_tokens(test_config: Config):
    """Test that _edit_llm_settings handles invalid max_tokens."""
    with (
        patch("src.ui.options_tui.questionary.select") as mock_select,
        patch("src.ui.options_tui.questionary.text") as mock_text,
    ):
        # Select max_tokens, enter invalid value, then cancel
        mock_select.return_value.ask.side_effect = [
            "max_tokens",  # Select Max Tokens field
            "__cancel__",  # Cancel after error
        ]

        # Invalid max_tokens input
        mock_text.return_value.ask.return_value = "not-an-integer"

        result = _edit_llm_settings(test_config)
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _edit_whatsapp_settings
# ─────────────────────────────────────────────────────────────────────────────


def test_edit_whatsapp_settings_applies_changes(test_config: Config):
    """Test that _edit_whatsapp_settings applies input changes."""
    with (
        patch("src.ui.options_tui.questionary.select") as mock_select,
        patch("src.ui.options_tui.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.side_effect = [
            "db_path",  # Select DB Path field
            "__done__",  # Done
        ]
        mock_text.return_value.ask.return_value = "workspace/custom_session.db"

        result = _edit_whatsapp_settings(test_config)

        assert result is True
        assert test_config.whatsapp.neonize.db_path == "workspace/custom_session.db"


def test_edit_whatsapp_settings_cancel_returns_false(test_config: Config):
    """Test that _edit_whatsapp_settings returns False when cancelled."""
    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "__cancel__"

        result = _edit_whatsapp_settings(test_config)

        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _edit_general_settings
# ─────────────────────────────────────────────────────────────────────────────


def test_edit_general_settings_applies_changes(test_config: Config):
    """Test that _edit_general_settings applies input changes."""
    with (
        patch("src.ui.options_tui.questionary.select") as mock_select,
        patch("src.ui.options_tui.questionary.text") as mock_text,
    ):
        mock_select.return_value.ask.side_effect = [
            "memory_max_history",  # Select memory_max_history field
            "__done__",  # Done
        ]
        mock_text.return_value.ask.return_value = "100"

        result = _edit_general_settings(test_config)

        assert result is True
        assert test_config.memory_max_history == 100


def test_edit_general_settings_cancel_returns_false(test_config: Config):
    """Test that _edit_general_settings returns False when cancelled."""
    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "__cancel__"

        result = _edit_general_settings(test_config)

        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# Tests: run_options_tui
# ─────────────────────────────────────────────────────────────────────────────


def test_run_options_tui_save_success(config_file: Path):
    """Test successful configuration save via TUI."""
    menu_sequence = ["llm", "save"]  # Navigate to LLM settings, then save

    with (
        patch("src.ui.options_tui._show_main_menu") as mock_menu,
        patch("src.ui.options_tui._edit_llm_settings") as mock_edit_llm,
        patch("src.ui.options_tui.questionary.password") as mock_password,
        patch("src.ui.options_tui.questionary.text") as mock_text,
    ):
        mock_menu.side_effect = menu_sequence
        mock_edit_llm.return_value = True
        mock_password.return_value.ask.return_value = "sk-new-key"
        mock_text.side_effect = ["gpt-4o", "https://api.openai.com/v1", "0.7", "500"]

        result = run_options_tui(config_file)

        assert result is True


def test_run_options_tui_cancel(config_file: Path):
    """Test TUI cancellation."""
    with patch("src.ui.options_tui._show_main_menu") as mock_menu:
        mock_menu.return_value = "cancel"

        result = run_options_tui(config_file)

        assert result is False


def test_run_options_tui_escape_returns_none(config_file: Path):
    """Test TUI escape (None selection) cancels."""
    with patch("src.ui.options_tui._show_main_menu") as mock_menu:
        mock_menu.return_value = None

        result = run_options_tui(config_file)

        assert result is False


def test_run_options_tui_config_load_failure(temp_config_path: Path):
    """Test TUI handles config load failure gracefully."""
    # Don't create the config file - simulate load failure
    with patch("src.ui.options_tui.load_config") as mock_load:
        mock_load.side_effect = FileNotFoundError("Config not found")

        result = run_options_tui(temp_config_path)

        assert result is False


def test_run_options_tui_save_failure(config_file: Path):
    """Test TUI handles save failure gracefully."""
    with (
        patch("src.ui.options_tui._show_main_menu") as mock_menu,
        patch("src.ui.options_tui.save_config") as mock_save,
    ):
        mock_menu.return_value = "save"
        mock_save.side_effect = PermissionError("Cannot write file")

        result = run_options_tui(config_file)

        assert result is False


def test_run_options_tui_navigates_multiple_menus(config_file: Path):
    """Test TUI navigation through multiple menu selections."""
    menu_sequence = ["llm", "whatsapp", "general", "save"]

    with (
        patch("src.ui.options_tui._show_main_menu") as mock_menu,
        patch("src.ui.options_tui._edit_llm_settings") as mock_llm,
        patch("src.ui.options_tui._edit_whatsapp_settings") as mock_whatsapp,
        patch("src.ui.options_tui._edit_general_settings") as mock_general,
    ):
        mock_menu.side_effect = menu_sequence
        mock_llm.return_value = True
        mock_whatsapp.return_value = True
        mock_general.return_value = True

        result = run_options_tui(config_file)

        assert result is True
        assert mock_llm.called
        assert mock_whatsapp.called
        assert mock_general.called


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Edge cases
# ─────────────────────────────────────────────────────────────────────────────


def test_run_options_tui_empty_config_file(temp_config_path: Path):
    """Test TUI handles empty config file."""
    # Create an empty config file
    with open(temp_config_path, "w") as f:
        f.write("")

    # This should fail to load the config
    with patch("src.ui.options_tui.load_config") as mock_load:
        mock_load.side_effect = json.JSONDecodeError("Empty file", "", 0)

        result = run_options_tui(temp_config_path)

        assert result is False


def test_run_options_tui_invalid_json_config(temp_config_path: Path):
    """Test TUI handles invalid JSON config."""
    # Create an invalid JSON config file
    with open(temp_config_path, "w") as f:
        f.write("{ invalid json }")

    with patch("src.ui.options_tui.load_config") as mock_load:
        mock_load.side_effect = json.JSONDecodeError("Invalid JSON", "", 0)

        result = run_options_tui(temp_config_path)

        assert result is False


def test_edit_llm_settings_preserves_original_on_cancel(test_config: Config):
    """Test that cancelling LLM edit preserves original values."""
    original_key = test_config.llm.api_key
    original_model = test_config.llm.model

    with patch("src.ui.options_tui.questionary.select") as mock_select:
        mock_select.return_value.ask.return_value = "__cancel__"  # Cancel

        _edit_llm_settings(test_config)

        # Original values should be preserved
        assert test_config.llm.api_key == original_key
        assert test_config.llm.model == original_model
