"""
src/ui/options_tui.py — TUI module for configuration editing using questionary.

Provides an interactive terminal interface for editing configuration settings:
- LLM settings (api_key, model, base_url, temperature)
- WhatsApp settings (bridge_url)
- General settings (workspace)

Uses a list-based interface where all fields are displayed and users navigate
with arrow keys to select which field to edit.

Usage:
    from src.ui.options_tui import run_options_tui

    if run_options_tui():
        print("Configuration saved successfully")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, List, Optional

import questionary

from src.config import CONFIG_PATH, Config, load_config, save_config
from src.ui.cli_output import cli

# Custom style for questionary prompts
QUESTIONARY_STYLE = questionary.Style(
    [
        ("selected", "fg:cyan bold"),
        ("highlighted", "fg:cyan bold"),
        ("pointer", "fg:cyan bold"),
        ("qmark", "fg:cyan bold"),
    ]
)


def _truncate_value(value: Any, max_length: int = 40) -> str:
    """Truncate a value for display in the menu."""
    s = str(value)
    if len(s) > max_length:
        return s[: max_length - 3] + "..."
    return s


def _mask_api_key(api_key: str) -> str:
    """Mask API key for display, showing only first 4 and last 4 chars."""
    if not api_key:
        return "(not set)"
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}...{api_key[-4:]}"


def _edit_field_list(
    title: str,
    fields: List[dict],
    config_obj: Any,
) -> bool:
    """
    Generic list-based field editor.

    Args:
        title: Header title to display
        fields: List of field definitions with keys:
            - name: Display name
            - attr: Attribute name on config_obj
            - type: "text", "password", "int", or "float"
            - format: Optional formatting function for display
        config_obj: The config object to edit

    Returns:
        True if changes were made, False otherwise
    """
    changed = False

    while True:
        cli.header(title)

        # Build choices with current values
        choices = []
        for field in fields:
            current_value = getattr(config_obj, field["attr"])
            formatter = field.get("format", _truncate_value)

            if field["type"] == "password":
                display_value = _mask_api_key(current_value)
            else:
                display_value = formatter(current_value)

            choices.append(
                questionary.Choice(
                    f"{field['name']}: {display_value}",
                    value=field["attr"],
                )
            )

        # Add navigation options
        choices.append(questionary.Choice("─────────────────────", value=None, disabled=True))
        choices.append(questionary.Choice("✓ Done (save changes)", value="__done__"))
        choices.append(questionary.Choice("✗ Cancel", value="__cancel__"))

        # Show the selection menu
        choice = questionary.select(
            "Select a field to edit:",
            choices=choices,
            style=QUESTIONARY_STYLE,
        ).ask()

        if choice is None or choice == "__cancel__":
            return changed

        if choice == "__done__":
            return changed

        # Find the field definition and prompt for new value
        for field in fields:
            if field["attr"] == choice:
                current_value = getattr(config_obj, field["attr"])

                if field["type"] == "password":
                    new_value = questionary.password(
                        f"Enter new {field['name']}:",
                        default=current_value,
                        style=QUESTIONARY_STYLE,
                    ).ask()
                elif field["type"] == "int":
                    new_value_str = questionary.text(
                        f"Enter new {field['name']}:",
                        default=str(current_value),
                        style=QUESTIONARY_STYLE,
                    ).ask()
                    if new_value_str is not None:
                        try:
                            new_value = int(new_value_str)
                        except ValueError:
                            cli.error("Invalid integer value")
                            continue
                    else:
                        continue
                elif field["type"] == "float":
                    new_value_str = questionary.text(
                        f"Enter new {field['name']}:",
                        default=str(current_value),
                        style=QUESTIONARY_STYLE,
                    ).ask()
                    if new_value_str is not None:
                        try:
                            new_value = float(new_value_str)
                        except ValueError:
                            cli.error("Invalid numeric value")
                            continue
                    else:
                        continue
                else:  # text
                    new_value = questionary.text(
                        f"Enter new {field['name']}:",
                        default=str(current_value),
                        style=QUESTIONARY_STYLE,
                    ).ask()

                if new_value is not None:
                    setattr(config_obj, field["attr"], new_value)
                    changed = True
                    cli.success(f"{field['name']} updated")

                break

    return changed


def _edit_llm_settings(config: Config) -> bool:
    """Edit LLM configuration settings with list-based navigation."""
    fields = [
        {"name": "API Key", "attr": "api_key", "type": "password"},
        {"name": "Model", "attr": "model", "type": "text"},
        {"name": "Base URL", "attr": "base_url", "type": "text"},
        {"name": "Temperature", "attr": "temperature", "type": "float"},
    ]

    return _edit_field_list("LLM Settings", fields, config.llm)


def _edit_whatsapp_settings(config: Config) -> bool:
    """Edit WhatsApp configuration settings with list-based navigation."""
    fields = [
        {"name": "Session DB Path", "attr": "db_path", "type": "text"},
    ]

    return _edit_field_list("WhatsApp Settings", fields, config.whatsapp.neonize)


def _edit_general_settings(config: Config) -> bool:
    """Edit general configuration settings with list-based navigation."""
    fields = [
        {"name": "Workspace Directory", "attr": "workspace", "type": "text"},
        {"name": "Memory Max History", "attr": "memory_max_history", "type": "int"},
        {"name": "Skills Auto Load", "attr": "skills_auto_load", "type": "text"},
        {
            "name": "Skills User Directory",
            "attr": "skills_user_directory",
            "type": "text",
        },
    ]

    # Handle boolean field specially
    changed = False

    while True:
        cli.header("General Settings")

        # Build choices with current values
        choices = [
            questionary.Choice(
                f"Memory Max History: {config.memory_max_history}",
                value="memory_max_history",
            ),
            questionary.Choice(
                f"Skills Auto Load: {config.skills_auto_load}",
                value="skills_auto_load",
            ),
            questionary.Choice(
                f"Skills User Directory: {_truncate_value(config.skills_user_directory)}",
                value="skills_user_directory",
            ),
            questionary.Choice("─────────────────────", value=None, disabled=True),
            questionary.Choice("✓ Done (save changes)", value="__done__"),
            questionary.Choice("✗ Cancel", value="__cancel__"),
        ]

        choice = questionary.select(
            "Select a field to edit:",
            choices=choices,
            style=QUESTIONARY_STYLE,
        ).ask()

        if choice is None or choice == "__cancel__":
            return changed

        if choice == "__done__":
            return changed

        current_value = getattr(config, choice)

        if choice == "skills_auto_load":
            # Boolean toggle
            new_value = questionary.confirm(
                "Enable Skills Auto Load?",
                default=current_value,
                style=QUESTIONARY_STYLE,
            ).ask()
        else:
            new_value_str = questionary.text(
                f"Enter new value:",
                default=str(current_value),
                style=QUESTIONARY_STYLE,
            ).ask()

            if new_value_str is None:
                continue

            if choice == "memory_max_history":
                try:
                    new_value = int(new_value_str)
                except ValueError:
                    cli.error("Invalid integer value")
                    continue
            else:
                new_value = new_value_str

        if new_value is not None:
            setattr(config, choice, new_value)
            changed = True
            cli.success(f"{choice.replace('_', ' ').title()} updated")

    # Should never reach here
    return changed


def _show_main_menu() -> Optional[str]:
    """Show the main options menu. Returns selected action or None if cancelled."""
    choices = [
        questionary.Choice(
            "LLM Settings (api_key, model, base_url, temperature)",
            value="llm",
        ),
        questionary.Choice("WhatsApp Settings (bridge_url)", value="whatsapp"),
        questionary.Choice("General Settings (workspace)", value="general"),
        questionary.Choice("Save and Exit", value="save"),
        questionary.Choice("Cancel", value="cancel"),
    ]

    return questionary.select(
        "What would you like to configure?",
        choices=choices,
        style=questionary.Style(
            [
                ("selected", "fg:cyan bold"),
                ("highlighted", "fg:cyan bold"),
                ("pointer", "fg:cyan bold"),
            ]
        ),
    ).ask()


def run_options_tui(config_path: Path = CONFIG_PATH) -> bool:
    """
    Run the options configuration TUI.

    Provides an interactive menu for editing configuration settings.
    Changes are saved to config.json when the user selects "Save and Exit".

    Args:
        config_path: Path to the configuration file (default: config.json).

    Returns:
        True if configuration was saved successfully, False otherwise.
    """
    cli.banner("Configuration Editor", "Edit your CustomBot settings")

    # Load current configuration
    try:
        config = load_config(config_path)
        cli.info(f"Loaded configuration from {config_path}")
    except Exception as e:
        cli.error(f"Failed to load configuration: {e}")
        return False

    # Main configuration loop
    while True:
        choice = _show_main_menu()

        if choice is None or choice == "cancel":
            cli.info("Configuration cancelled")
            return False

        elif choice == "llm":
            _edit_llm_settings(config)

        elif choice == "whatsapp":
            _edit_whatsapp_settings(config)

        elif choice == "general":
            _edit_general_settings(config)

        elif choice == "save":
            try:
                save_config(config, config_path)
                cli.success(f"Configuration saved to {config_path}")
                return True
            except Exception as e:
                cli.error(f"Failed to save configuration: {e}")
                return False

    # Should never reach here, but satisfy type checker
    return False
