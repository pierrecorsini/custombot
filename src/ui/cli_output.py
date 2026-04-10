"""
src/ui/cli_output.py — Centralized colorful CLI output utilities.

Provides styled console output using Rich library with:
- Status emojis (success, error, warning, info, loading, WhatsApp, bot)
- Consistent color scheme (green=success, red=error, yellow=warning, cyan=info)
- Styled headers and separators
- Cross-platform support (Windows, Linux, macOS)
- Non-TTY detection for automatic color stripping

Usage:
    from src.ui.cli_output import cli

    cli.success("Operation completed!")
    cli.error("Something went wrong")
    cli.header("Section Title")
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Optional

from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.style import Style
from rich.text import Text
from rich.theme import Theme

# ─────────────────────────────────────────────────────────────────────────────
# Theme Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Define a consistent theme with semantic style names
CLI_THEME = Theme(
    {
        # Status styles
        "success": "bold green",
        "error": "bold red",
        "warning": "bold yellow",
        "info": "cyan",
        "loading": "yellow",
        # Context styles
        "whatsapp": "green",
        "bot": "bold blue",
        "dim": "dim",
        "highlight": "bold cyan",
        # Component styles
        "header": "bold cyan",
        "label": "bold",
        "value": "cyan",
        "path": "underline cyan",
        # Message flow styles
        "msg.timestamp": "dim cyan",
        "msg.channel": "cyan",
        "msg.direction_in": "bold yellow",
        "msg.direction_out": "bold green",
        "msg.source": "bold",
        "msg.arrow_in": "yellow",
        "msg.arrow_out": "green",
        "msg.destination": "cyan",
        "msg.flags": "dim",
        "msg.text_in": "white",
        "msg.text_out": "green",
    }
)

# Max characters of message text to display
MSG_PREVIEW_LENGTH = 120

# ─────────────────────────────────────────────────────────────────────────────
# Emoji Constants
# ─────────────────────────────────────────────────────────────────────────────

# Status emojis
EMOJI_SUCCESS = "✅"
EMOJI_ERROR = "❌"
EMOJI_WARNING = "⚠️"
EMOJI_INFO = "ℹ️"
EMOJI_LOADING = "🔄"
EMOJI_WHATSAPP = "📱"
EMOJI_BOT = "🤖"
EMOJI_CHECK = "✓"
EMOJI_CROSS = "✗"
EMOJI_ROCKET = "🚀"
EMOJI_GEAR = "⚙️"
EMOJI_SPARKLE = "✨"


# ─────────────────────────────────────────────────────────────────────────────
# CLI Output Class
# ─────────────────────────────────────────────────────────────────────────────


class CLIOutput:
    """
    Centralized CLI output handler with styled messages and emojis.

    Features:
    - Consistent color scheme across the application
    - Status emojis for visual feedback
    - Styled headers, panels, and separators
    - Automatic non-TTY detection and color stripping
    - Cross-platform compatibility

    Usage:
        cli = CLIOutput()
        cli.success("File saved successfully")
        cli.error("Connection failed")
        cli.header("Configuration")
    """

    def __init__(self, force_color: Optional[bool] = None) -> None:
        """
        Initialize CLI output handler.

        Args:
            force_color: If True, force colors even in non-TTY.
                        If False, disable colors.
                        If None, auto-detect based on terminal.
        """
        # Determine color system based on environment
        color_system = "auto"
        if force_color is False:
            color_system = None
        elif force_color is True:
            color_system = "truecolor"

        self.console = Console(theme=CLI_THEME, color_system=color_system)
        self._is_tty = self.console.is_terminal

    @property
    def is_terminal(self) -> bool:
        """Check if output is going to a terminal."""
        return self._is_tty

    # ─────────────────────────────────────────────────────────────────────────
    # Status Messages
    # ─────────────────────────────────────────────────────────────────────────

    def success(self, message: str, emoji: bool = True) -> None:
        """Print a success message with green styling and checkmark emoji."""
        prefix = f"{EMOJI_SUCCESS} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[success]{message}[/success]")

    def error(self, message: str, emoji: bool = True) -> None:
        """Print an error message with red styling and X emoji."""
        prefix = f"{EMOJI_ERROR} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[error]{message}[/error]")

    def warning(self, message: str, emoji: bool = True) -> None:
        """Print a warning message with yellow styling and warning emoji."""
        prefix = f"{EMOJI_WARNING} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[warning]{message}[/warning]")

    def info(self, message: str, emoji: bool = True) -> None:
        """Print an info message with cyan styling and info emoji."""
        prefix = f"{EMOJI_INFO} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[info]{message}[/info]")

    def loading(self, message: str, emoji: bool = True) -> None:
        """Print a loading/processing message with yellow styling and spinner emoji."""
        prefix = f"{EMOJI_LOADING} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[loading]{message}[/loading]")

    # ─────────────────────────────────────────────────────────────────────────
    # Context-Specific Messages
    # ─────────────────────────────────────────────────────────────────────────

    def whatsapp(self, message: str, emoji: bool = True) -> None:
        """Print a WhatsApp-related message with green styling and phone emoji."""
        prefix = f"{EMOJI_WHATSAPP} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[whatsapp]{message}[/whatsapp]")

    def bot(self, message: str, emoji: bool = True) -> None:
        """Print a bot-related message with magenta styling and robot emoji."""
        prefix = f"{EMOJI_BOT} " if emoji and self._is_tty else ""
        self.console.print(f"{prefix}[bot]{message}[/bot]")

    # ─────────────────────────────────────────────────────────────────────────
    # Styled Components
    # ─────────────────────────────────────────────────────────────────────────

    def header(self, title: str, style: str = "header") -> None:
        """Print a styled header with optional box characters."""
        if self._is_tty:
            self.console.print(
                f"\n[bold]── {title} ──[/bold]", style=style, justify="left"
            )
        else:
            self.console.print(f"\n=== {title} ===")

    def separator(self, char: str = "─", length: int = 60) -> None:
        """Print a horizontal separator line."""
        self.console.print(char * length, style="dim")

    def rule(self, title: Optional[str] = None, style: str = "cyan") -> None:
        """Print a styled horizontal rule with optional title."""
        if title:
            self.console.rule(f"[{style}]{title}[/{style}]", style=style)
        else:
            self.console.rule(style=style)

    def panel(
        self,
        content: str,
        title: Optional[str] = None,
        style: str = "cyan",
        expand: bool = False,
    ) -> None:
        """Print content in a styled panel with border."""
        self.console.print(
            Panel(content, title=title, border_style=style, expand=expand)
        )

    def banner(self, title: str, subtitle: Optional[str] = None) -> None:
        """Print a large banner/header with box drawing characters."""
        if self._is_tty:
            lines = ["", f"╔{'═' * 40}╗"]
            lines.append(f"║{title.center(40)}║")
            if subtitle:
                lines.append(f"║{subtitle.center(40)}║")
            lines.append(f"╚{'═' * 40}╝")
            self.console.print("\n".join(lines), style="bold cyan")
        else:
            self.console.print(f"\n=== {title} ===")
            if subtitle:
                self.console.print(f"    {subtitle}")

    # ─────────────────────────────────────────────────────────────────────────
    # Message Flow Logging
    # ─────────────────────────────────────────────────────────────────────────

    def log_message_flow(
        self,
        direction: str,
        channel: str,
        source: str,
        destination: str,
        text: str,
        from_me: bool = False,
        to_me: bool = False,
    ) -> None:
        """
        Print a structured one-line message flow log.

        Format:
            [HH:MM:SS][channel][IN] source → destination (fromMe=X/toMe=X): text...
            [HH:MM:SS][channel][OUT] source ← destination (fromMe=X/toMe=X): text...

        Args:
            direction: "IN" for incoming, "OUT" for outgoing.
            channel: Channel identifier (e.g. "whatsapp", "cli").
            source: Sender identifier (sender_name or "Bot").
            destination: Target identifier (chat_id).
            text: Message body (truncated to MSG_PREVIEW_LENGTH).
            from_me: Whether message was sent by the bot user.
            to_me: Whether message was sent directly to the bot.
        """
        timestamp = datetime.now().strftime("%H:%M:%S")
        is_in = direction.upper() == "IN"
        arrow = "→" if is_in else "←"
        preview = text.replace("\n", "\\n")[:MSG_PREVIEW_LENGTH]
        dir_tag = "IN" if is_in else "OUT"

        if self._is_tty:
            dir_style = "msg.direction_in" if is_in else "msg.direction_out"
            arrow_style = "msg.arrow_in" if is_in else "msg.arrow_out"
            text_style = "msg.text_in" if is_in else "msg.text_out"

            self.console.print(
                f"[msg.timestamp][{timestamp}][/]"
                f"[msg.channel][{channel}][/]"
                f"[{dir_style}][{dir_tag}][/]"
                f" [msg.source]{source}[/]"
                f" [{arrow_style}]{arrow}[/]"
                f" [msg.destination]{destination}[/]"
                f" [msg.flags](fromMe={from_me}/toMe={to_me})[/]:"
                f" [{text_style}]{preview}[/]"
            )
        else:
            self.console.print(
                f"[{timestamp}][{channel}][{dir_tag}] "
                f"{source} {arrow} {destination} "
                f"(fromMe={from_me}/toMe={to_me}): {preview}"
            )

    def print(self, message: str, style: Optional[str] = None) -> None:
        """Print a message with optional style."""
        if style:
            self.console.print(f"[{style}]{message}[/{style}]")
        else:
            self.console.print(message)

    def dim(self, message: str) -> None:
        """Print a dimmed message (less prominent)."""
        self.console.print(f"[dim]{message}[/dim]")

    def highlight(self, message: str) -> None:
        """Print a highlighted message."""
        self.console.print(f"[highlight]{message}[/highlight]")

    def kv(self, key: str, value: str, key_width: int = 20) -> None:
        """Print a key-value pair with aligned formatting."""
        padded_key = key.ljust(key_width)
        self.console.print(f"  [label]{padded_key}[/label] = [value]{value}[/value]")

    def bullet(self, message: str, style: str = "dim") -> None:
        """Print a bullet point item."""
        self.console.print(f"  • [{style}]{message}[/{style}]")

    def check(self, message: str, checked: bool = True) -> None:
        """Print a checkmark or X item."""
        if checked:
            self.console.print(f"  [green]{EMOJI_CHECK}[/green] {message}")
        else:
            self.console.print(f"  [red]{EMOJI_CROSS}[/red] {message}")

    def raw(self, *args, **kwargs) -> None:
        """Pass through to underlying console.print for complex output."""
        self.console.print(*args, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Global Instance
# ─────────────────────────────────────────────────────────────────────────────

# Create a global instance for easy import
cli = CLIOutput()


def log_message_flow(
    direction: str,
    channel: str,
    source: str,
    destination: str,
    text: str,
    from_me: bool = False,
    to_me: bool = False,
) -> None:
    """
    Convenience wrapper for cli.log_message_flow().

    See CLIOutput.log_message_flow for full documentation.
    """
    cli.log_message_flow(
        direction=direction,
        channel=channel,
        source=source,
        destination=destination,
        text=text,
        from_me=from_me,
        to_me=to_me,
    )


def get_cli(force_color: Optional[bool] = None) -> CLIOutput:
    """
    Get a CLI output instance with custom color settings.

    Args:
        force_color: Override auto-detection for color output.

    Returns:
        CLIOutput instance with specified settings.
    """
    return CLIOutput(force_color=force_color)
