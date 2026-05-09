"""
src/ui/progress.py — Progressive response delivery for long-running operations.

Sends intermediate progress updates to the user while tools or skills
are executing. Rate-limited to avoid spamming the chat.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from src.channels.base import BaseChannel

log = logging.getLogger(__name__)

# Minimum seconds between progress updates for the same chat.
MIN_PROGRESS_INTERVAL = 3.0

# Standard progress messages keyed by phase.
PROGRESS_MESSAGES: dict[str, str] = {
    "searching": "Searching...",
    "found": "Found {count} results, analyzing...",
    "processing": "Processing...",
    "analyzing": "Analyzing results...",
    "almost_done": "Almost done...",
    "generating": "Generating response...",
}


@dataclass(slots=True)
class ProgressState:
    """Tracks rate-limit state per chat."""

    last_sent: float = 0.0


class ProgressReporter:
    """Send rate-limited progress updates during long operations."""

    def __init__(self) -> None:
        self._states: dict[str, ProgressState] = {}

    async def report_progress(
        self,
        chat_id: str,
        message: str,
        channel: BaseChannel,
        step: int = 0,
        total_steps: int = 0,
    ) -> None:
        """Send a progress update if rate-limit allows.

        Args:
            chat_id: Target chat.
            message: Progress text to send.
            channel: Channel to send through.
            step: Current step number (1-based).
            total_steps: Total number of steps.
        """
        state = self._state_for(chat_id)
        now = time.monotonic()

        if now - state.last_sent < MIN_PROGRESS_INTERVAL:
            return

        state.last_sent = now

        prefix = f"[{step}/{total_steps}] " if total_steps > 0 else ""
        try:
            await channel.send_message(chat_id, f"⏳ {prefix}{message}")
        except Exception:
            log.debug("Failed to send progress to %s", chat_id, exc_info=True)

    def format_message(self, phase: str, **kwargs: Any) -> str:
        """Format a standard progress message.

        Args:
            phase: Key from PROGRESS_MESSAGES.
            **kwargs: Format parameters (e.g., count="5").

        Returns:
            Formatted message string.
        """
        template = PROGRESS_MESSAGES.get(phase, phase)
        return template.format(**kwargs)

    def reset(self, chat_id: str) -> None:
        """Clear rate-limit state for a chat (e.g., on operation completion)."""
        self._states.pop(chat_id, None)

    def _state_for(self, chat_id: str) -> ProgressState:
        if chat_id not in self._states:
            self._states[chat_id] = ProgressState()
        return self._states[chat_id]


# Module-level convenience for creating progress callbacks for tool execution.
ProgressCallback = Callable[[str, int, int], Awaitable[None]]


def make_progress_callback(
    reporter: ProgressReporter,
    chat_id: str,
    channel: BaseChannel,
) -> ProgressCallback:
    """Create a progress callback suitable for ToolExecutor integration.

    Returns:
        Async callable accepting (message, step, total_steps).
    """
    async def _callback(message: str, step: int, total_steps: int) -> None:
        await reporter.report_progress(chat_id, message, channel, step, total_steps)

    return _callback
