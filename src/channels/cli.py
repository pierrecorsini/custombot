"""
channels/cli.py — Command Line Interface channel.

Provides an interactive terminal-based chat interface for the bot,
allowing users to interact without WhatsApp or the Node.js bridge.

Usage:
    python main.py cli
    python main.py cli --chat-id my-session
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

from src.channels.base import BaseChannel, IncomingMessage, MessageHandler

log = logging.getLogger(__name__)

# Create a console instance for colorful output
console = Console()


class CommandLineChannel(BaseChannel):
    """
    Interactive command line channel for terminal-based bot interaction.

    Provides a REPL-style interface where users can type messages and
    receive responses directly in the terminal with colorful output.
    """

    def __init__(
        self,
        chat_id: str = "cli",
        sender_name: str = "User",
        prompt: str = "You: ",
        safe_mode: bool = False,
    ) -> None:
        super().__init__(safe_mode=safe_mode)
        self._chat_id = chat_id
        self._sender_name = sender_name
        self._prompt = prompt
        self._running = False
        self._response_queue: asyncio.Queue[str] = asyncio.Queue()

    def get_channel_prompt(self) -> str | None:
        """
        Return CLI-specific instructions.

        CLI mode uses Rich formatting for terminal output.
        """
        return """## CLI Mode

You are in terminal/CLI mode. Format responses for readability in a text terminal:
- Use clear section headers
- Keep lines reasonably short (avoid very long lines)
- Use code blocks for code or commands"""

    async def start(self, handler: MessageHandler) -> None:
        """Start the interactive REPL loop."""
        self._running = True
        log.info("CommandLine channel starting (chat_id=%s)", self._chat_id)

        # Print colorful welcome banner
        console.print("")
        console.rule("[bold cyan]🤖 CustomBot CLI Mode[/bold cyan]")
        console.print(
            Panel(
                "[dim]Type 'exit' or 'quit' to stop, Ctrl+C to interrupt[/dim]",
                border_style="dim",
                padding=(0, 2),
            )
        )
        console.print("")

        # Run the input loop in a separate thread to avoid blocking
        loop = asyncio.get_event_loop()

        while self._running:
            try:
                # Get input in executor to not block the event loop
                user_input = await loop.run_in_executor(None, self._read_input)

                if user_input is None:
                    # EOF or error
                    break

                text = user_input.strip()
                if not text:
                    continue

                # Check for exit commands
                if text.lower() in ("exit", "quit", "q"):
                    console.print("\n[bold green]👋 Goodbye![/bold green]\n")
                    break

                # Create incoming message and pass to handler
                msg = IncomingMessage(
                    message_id=str(uuid.uuid4()),
                    chat_id=self._chat_id,
                    sender_id=self._chat_id,
                    sender_name=self._sender_name,
                    text=text,
                    timestamp=time.time(),
                    channel_type="cli",
                    fromMe=False,
                    toMe=True,  # CLI is always a direct message to the bot
                    correlation_id=str(uuid.uuid4())[
                        :8
                    ],  # Generate correlation ID for CLI
                )

                # Create stream callback for real-time tool execution logging
                async def stream_callback(response_text: str) -> None:
                    self._print_response(response_text)

                # Process the message with channel and stream callback
                try:
                    response = await handler(
                        msg, channel=self, stream_callback=stream_callback
                    )
                    if response:
                        self._print_response(response)
                except Exception as exc:
                    log.error("Error handling message: %s", exc)
                    console.print(f"\n[bold red]❌ Error:[/bold red] {exc}\n")

            except KeyboardInterrupt:
                console.print(
                    "\n\n[yellow]⚠️ Interrupted.[/yellow] [bold green]Goodbye![/bold green]\n"
                )
                break
            except Exception as exc:
                log.error("CLI loop error: %s", exc)
                console.print(f"\n[bold red]❌ Error:[/bold red] {exc}\n")

        self._running = False

    def _read_input(self) -> Optional[str]:
        """Read input from stdin (blocking, runs in executor)."""
        try:
            # Use Rich for styled prompt if terminal supports it
            if console.is_terminal:
                console.print(f"[bold cyan]You:[/bold cyan] ", end="")
                return input()
            return input(self._prompt)
        except EOFError:
            return None
        except Exception:
            return None

    def _print_response(self, text: str) -> None:
        """Print the bot's response with colorful styling."""
        console.print(f"\n[bold green]🤖 Bot:[/bold green] {text}\n")

    async def _send_message(self, chat_id: str, text: str) -> None:
        """
        Send a message (for CLI, this is handled by the handler response).

        In CLI mode, responses are printed directly via the handler return value.
        This method exists to satisfy the BaseChannel interface.
        """
        self._print_response(text)

    async def send_audio(
        self, chat_id: str, file_path: Path, *, ptt: bool = False
    ) -> None:
        """Print audio file info in CLI mode."""
        kind = "voice note" if ptt else "audio"
        console.print(
            f"\n[bold magenta]🔊 {kind.title()}:[/bold magenta] {file_path}\n"
        )

    async def send_document(
        self,
        chat_id: str,
        file_path: Path,
        *,
        caption: str = "",
        filename: str = "",
    ) -> None:
        """Print document file info in CLI mode."""
        label = filename or file_path.name
        cap = f" — {caption}" if caption else ""
        console.print(f"\n[bold magenta]📄 Document:[/bold magenta] {label}{cap}\n")

    async def send_typing(self, chat_id: str) -> None:
        """Show typing indicator (no-op for CLI)."""
        pass

    def stop(self) -> None:
        """Signal the channel to stop."""
        self._running = False
