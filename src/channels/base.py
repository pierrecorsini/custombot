"""
channels/base.py — Abstract channel interface.

All channels (WhatsApp, future Telegram, etc.) implement this contract so
bot.py never needs to know which transport is in use.

The BaseChannel ABC provides a concrete base class for channel implementations,
while the Channel Protocol in src/protocols.py enables structural subtyping
for any class that implements the required methods.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from src.utils.protocols import Channel

log = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class IncomingMessage:
    """
    Immutable, normalised representation of an incoming message from any channel.

    Frozen dataclass: cannot be modified after creation. Enables use as
    dict keys and in sets. Thread-safe by design.

    Uses slots=True for memory efficiency - each message instance uses less memory
    and has faster attribute access. Important for high-volume message processing.

    Attributes:
        message_id: Unique ID provided by the channel (for dedup)
        chat_id: Conversation/group identifier
        sender_id: Sender's phone/user ID
        sender_name: Human-readable display name
        text: Message body (plain text)
        timestamp: Unix timestamp
        channel_type: Channel identifier (e.g., 'whatsapp', 'telegram')
        fromMe: True if message was sent by the bot user (from their number)
        toMe: True if message was sent directly to the bot user (not in a group)
        is_historical: True if message arrived before the bot connected (offline/backfill)
        correlation_id: Optional correlation ID for request tracing (from headers)
        raw: Original payload for debugging
    """

    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: float
    channel_type: str = ""
    fromMe: bool = False
    toMe: bool = False
    is_historical: bool = False
    correlation_id: Optional[str] = None
    raw: Optional[dict] = None

    def __repr__(self) -> str:
        text_preview = self.text[:40] + "..." if len(self.text) > 40 else self.text
        return (
            f"IncomingMessage(id={self.message_id[:12]!r}..., chat={self.chat_id!r}, "
            f"sender={self.sender_name!r}, text={text_preview!r}, channel={self.channel_type!r})"
        )


# Type alias for the callback the bot registers
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]


_safe_mode_lock = asyncio.Lock()


class BaseChannel(ABC):
    """
    Abstract async channel with optional safe mode.

    Safe mode (--safe) intercepts every outgoing message and prompts
    the user for Y/N confirmation before sending. Channels only need
    to implement _send_message(); send_message() is handled here.

    A module-level asyncio.Lock serialises all safe-mode prompts so
    that parallel message processing never interleaves preview output
    or Y/N prompts on the terminal.
    """

    def __init__(self, safe_mode: bool = False, load_history: bool = False) -> None:
        self._safe_mode = safe_mode
        self._load_history = load_history

    def get_channel_prompt(self) -> str | None:
        """
        Return channel-specific prompt instructions to inject before other prompts.

        Override this method to provide formatting or behavioral instructions
        specific to this channel (e.g., WhatsApp formatting, Telegram markdown).

        Returns:
            Channel-specific prompt content, or None if no prompt needed.
        """
        return None

    def should_process_historical(self, msg: IncomingMessage) -> bool:
        """
        Decide whether a historical (offline/backfill) message should be processed.

        Uses the global ``load_history`` config. Channels that need custom
        behaviour (e.g. interactive prompts) can override this method.

        Returns:
            True if the message should be handed to the bot, False to discard.
        """
        if not msg.is_historical:
            return True
        return self._load_history

    @abstractmethod
    async def start(self, handler: MessageHandler) -> None:
        """
        Start the channel.

        For polling channels (Green API) this enters an infinite loop.
        For webhook channels (Meta) this starts the HTTP server.
        Should run until cancelled.
        """
        ...

    async def send_message(
        self, chat_id: str, text: str, *, skip_delays: bool = False
    ) -> None:
        """Send a text reply. In safe mode, prompts Y/N before delegating.

        Args:
            chat_id: Target chat identifier.
            text: Message body.
            skip_delays: When True, bypass human-like timing delays
                (used for scheduled task delivery where no conversation context exists).
        """
        if self._safe_mode:
            preview = text[:120] + "..." if len(text) > 120 else text
            async with _safe_mode_lock:
                print(f"\n📤 Outgoing to {chat_id}:")
                print(f"   {preview}")
                if not await _confirm_send(chat_id):
                    log.info("Send cancelled by user (safe mode): %s", chat_id)
                    return
        await self._send_message(chat_id, text, skip_delays=skip_delays)

    @abstractmethod
    async def _send_message(
        self, chat_id: str, text: str, *, skip_delays: bool = False
    ) -> None:
        """Channel-specific send implementation (called by send_message)."""
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """
        Send a "typing…" indicator if the channel supports it.
        Implementations may be a no-op.
        """
        ...


async def _confirm_send(chat_id: str) -> bool:
    """Prompt Y/N for sending a message. Returns True to send."""
    while True:
        raw = await asyncio.to_thread(input, f"  Send to {chat_id}? [Y/N]: ")
        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        print("  Please enter Y or N.")
