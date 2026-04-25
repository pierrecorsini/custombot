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
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

if TYPE_CHECKING:
    from src.config import Config
    from src.utils.protocols import Channel

log = logging.getLogger(__name__)


class ChannelType(StrEnum):
    """Typed constants for known channel identifiers.

    ``StrEnum`` members are plain strings, so all existing string
    comparisons, routing-rule matching, and logging continue to work
    unchanged.  New channel implementations should add their identifier
    here and register it in ``VALID_CHANNEL_TYPES``.
    """

    WHATSAPP = "whatsapp"
    CLI = "cli"


# Known valid channel types.  Automatically includes every ``ChannelType``
# member plus the empty-string default.  Unknown alphanumeric strings are
# also accepted so that third-party / experimental channels work without
# code changes, but strings containing special characters (path separators,
# punctuation, etc.) are rejected to prevent injection into logs, metrics,
# or cache keys.
VALID_CHANNEL_TYPES: frozenset[str] = frozenset(e.value for e in ChannelType) | {""}

# Alphanumeric + underscore pattern for unknown-but-safe channel identifiers.
_CHANNEL_TYPE_RE = re.compile(r"^[a-z0-9_]+$")

# Pattern for valid chat_id at the message boundary.
# Allows: alphanumeric, dash, underscore, dot, and @.
# Real-world values: "1234567890@s.whatsapp.net", "120363abc@g.us",
# "12345678-1234-1234-1234-123456789012" (CLI UUID).
# Rejects: path separators (/ \), dots-only (.. traversal), control chars,
# whitespace, and other characters that could corrupt filesystem paths,
# log lines, or metric labels.
_CHAT_ID_RE = re.compile(r"^[a-zA-Z0-9_\-.@]+$")

# Pattern for valid message_id at the message boundary.
# Mirrors _CHAT_ID_RE: alphanumeric, dash, underscore, dot, @.
# Real-world values: WhatsApp "3EB0XXXXXX", CLI UUID "12345678-1234-1234-1234-123456789012".
_MESSAGE_ID_RE = _CHAT_ID_RE

# Pattern for valid sender_id at the message boundary.
# Mirrors _CHAT_ID_RE: alphanumeric, dash, underscore, dot, @.
# Real-world values: WhatsApp phone numbers "1234567890", CLI chat IDs "cli-abc123".
_SENDER_ID_RE = _CHAT_ID_RE


def _validate_chat_id(chat_id: object, *, max_length: int = 200) -> None:
    """Validate ``chat_id`` at the IncomingMessage boundary.

    Defense-in-depth check that catches malicious or malformed chat IDs
    *before* they reach any filesystem operation (workspace directories,
    JSONL files, scheduler paths, etc.).

    Args:
        chat_id: Value to validate.
        max_length: Maximum allowed length (default matches MAX_CHAT_ID_LENGTH).

    Raises:
        TypeError: If ``chat_id`` is not a string.
        ValueError: If ``chat_id`` is empty, too long, or contains unsafe
            characters.
    """
    from src.constants import MAX_CHAT_ID_LENGTH

    if not isinstance(chat_id, str):
        raise TypeError(
            f"IncomingMessage.chat_id must be a str, got {type(chat_id).__name__}"
        )
    if not chat_id:
        raise ValueError("IncomingMessage.chat_id must not be empty")
    effective_max = max_length or MAX_CHAT_ID_LENGTH
    if len(chat_id) > effective_max:
        raise ValueError(
            f"IncomingMessage.chat_id exceeds maximum length "
            f"({len(chat_id)} > {effective_max}): {chat_id[:40]!r}..."
        )
    if not _CHAT_ID_RE.match(chat_id):
        raise ValueError(
            f"IncomingMessage.chat_id contains invalid characters: {chat_id!r}. "
            "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
        )


def _validate_message_id(message_id: object, *, max_length: int = 200) -> None:
    """Validate ``message_id`` at the IncomingMessage boundary.

    Defense-in-depth check that catches malicious or malformed message IDs
    *before* they reach dedup indexes, log lines, or crash-recovery paths.

    Args:
        message_id: Value to validate.
        max_length: Maximum allowed length (default matches MAX_MESSAGE_ID_LENGTH).

    Raises:
        TypeError: If ``message_id`` is not a string.
        ValueError: If ``message_id`` is empty, too long, or contains unsafe
            characters.
    """
    from src.constants import MAX_MESSAGE_ID_LENGTH

    if not isinstance(message_id, str):
        raise TypeError(
            f"IncomingMessage.message_id must be a str, got {type(message_id).__name__}"
        )
    if not message_id:
        raise ValueError("IncomingMessage.message_id must not be empty")
    effective_max = max_length or MAX_MESSAGE_ID_LENGTH
    if len(message_id) > effective_max:
        raise ValueError(
            f"IncomingMessage.message_id exceeds maximum length "
            f"({len(message_id)} > {effective_max}): {message_id[:40]!r}..."
        )
    if not _MESSAGE_ID_RE.match(message_id):
        raise ValueError(
            f"IncomingMessage.message_id contains invalid characters: {message_id!r}. "
            "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
        )


def _validate_sender_id(sender_id: object, *, max_length: int = 200) -> None:
    """Validate ``sender_id`` at the IncomingMessage boundary.

    Defense-in-depth check that catches malicious or malformed sender IDs
    *before* they reach logs, audit trails, or metric labels.

    Args:
        sender_id: Value to validate.
        max_length: Maximum allowed length (default matches MAX_SENDER_ID_LENGTH).

    Raises:
        TypeError: If ``sender_id`` is not a string.
        ValueError: If ``sender_id`` is empty, too long, or contains unsafe
            characters.
    """
    from src.constants import MAX_SENDER_ID_LENGTH

    if not isinstance(sender_id, str):
        raise TypeError(
            f"IncomingMessage.sender_id must be a str, got {type(sender_id).__name__}"
        )
    if not sender_id:
        raise ValueError("IncomingMessage.sender_id must not be empty")
    effective_max = max_length or MAX_SENDER_ID_LENGTH
    if len(sender_id) > effective_max:
        raise ValueError(
            f"IncomingMessage.sender_id exceeds maximum length "
            f"({len(sender_id)} > {effective_max}): {sender_id[:40]!r}..."
        )
    if not _SENDER_ID_RE.match(sender_id):
        raise ValueError(
            f"IncomingMessage.sender_id contains invalid characters: {sender_id!r}. "
            "Only alphanumeric characters, dash, underscore, dot, and @ are allowed."
        )


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
        acl_passed: True if the channel verified the sender against its access-control
            list before dispatching.  ``handle_message()`` rejects messages where
            this is ``False`` — channels must set it to ``True`` after their ACL
            check passes (or for trusted channels like CLI).
        correlation_id: Optional correlation ID for request tracing (from headers)
        raw: Original payload for debugging
    """

    message_id: str
    chat_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: float
    channel_type: ChannelType | str = ""
    fromMe: bool = False
    toMe: bool = False
    is_historical: bool = False
    acl_passed: bool = False
    correlation_id: Optional[str] = None
    raw: Optional[dict] = None

    def __post_init__(self) -> None:
        # Defense-in-depth: validate identifiers before they reach any filesystem op.
        _validate_chat_id(self.chat_id)
        _validate_message_id(self.message_id)
        _validate_sender_id(self.sender_id)

        if self.channel_type in VALID_CHANNEL_TYPES:
            return
        if not isinstance(self.channel_type, str):
            raise TypeError(
                f"IncomingMessage.channel_type must be a str, got {type(self.channel_type).__name__}"
            )
        if not _CHANNEL_TYPE_RE.match(self.channel_type):
            raise ValueError(
                f"IncomingMessage.channel_type contains invalid characters: {self.channel_type!r}. "
                f"Must be one of {sorted(VALID_CHANNEL_TYPES - {''})!r} or a lowercase alphanumeric string."
            )

    def __repr__(self) -> str:
        text_preview = self.text[:40] + "..." if len(self.text) > 40 else self.text
        return (
            f"IncomingMessage(id={self.message_id[:12]!r}..., chat={self.chat_id!r}, "
            f"sender={self.sender_name!r}, text={text_preview!r}, channel={self.channel_type!r})"
        )


# Type alias for the callback the bot registers
MessageHandler = Callable[[IncomingMessage], Awaitable[None]]

# Type alias for the media-sending callback injected into skills.
# Callable(kind: "audio" | "document", path: Path, caption: str) -> Awaitable[None]
SendMediaCallback = Callable[[str, Path, str], Awaitable[None]]


_safe_mode_lock: asyncio.Lock | None = None


def _get_safe_mode_lock() -> asyncio.Lock:
    """Lazy-initialised asyncio.Lock for safe-mode serialisation.

    Cannot be created at module level because asyncio.Lock() requires
    a running event loop on Python 3.10+.
    """
    global _safe_mode_lock
    if _safe_mode_lock is None:
        _safe_mode_lock = asyncio.Lock()
    return _safe_mode_lock


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
        self._connected_event: asyncio.Event = asyncio.Event()

    def get_channel_prompt(self) -> str | None:
        """
        Return channel-specific prompt instructions to inject before other prompts.

        Override this method to provide formatting or behavioral instructions
        specific to this channel (e.g., WhatsApp formatting, Telegram markdown).

        Returns:
            Channel-specific prompt content, or None if no prompt needed.
        """
        return None

    def create_config_applier(self, **kwargs: object) -> object:
        """Return a config-change applier for hot-reload, or ``None``.

        Channels that support live config updates should override this
        and return an object with an ``apply(old_config, new_config)`` method.
        The base implementation returns ``None`` (no hot-reload support).
        """
        return None

    def apply_channel_config(self, new_config: Config, changed: set[str]) -> None:
        """Apply channel-specific config changes during hot-reload.

        Override in channel implementations to handle config field changes
        relevant to that channel. The base implementation is a no-op.

        Args:
            new_config: The full new configuration.
            changed: Set of dot-path field names that changed.
        """
        pass

    def mark_connected(self) -> None:
        """Signal that the channel has successfully connected."""
        self._connected_event.set()

    async def wait_connected(self) -> None:
        """Wait until the channel signals it is connected."""
        await self._connected_event.wait()

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

    async def send_message(self, chat_id: str, text: str, *, skip_delays: bool = False) -> None:
        """Send a text reply. In safe mode, prompts Y/N before delegating.

        Args:
            chat_id: Target chat identifier.
            text: Message body.
            skip_delays: When True, bypass human-like timing delays
                (used for scheduled task delivery where no conversation context exists).
        """
        if self._safe_mode:
            preview = text[:120] + "..." if len(text) > 120 else text
            async with _get_safe_mode_lock():
                print(f"\n📤 Outgoing to {chat_id}:")
                print(f"   {preview}")
                if not await _confirm_send(chat_id):
                    log.info("Send cancelled by user (safe mode): %s", chat_id)
                    return
        await self._send_message(chat_id, text, skip_delays=skip_delays)

    @abstractmethod
    async def _send_message(self, chat_id: str, text: str, *, skip_delays: bool = False) -> None:
        """Channel-specific send implementation (called by send_message)."""
        ...

    @abstractmethod
    async def send_typing(self, chat_id: str) -> None:
        """
        Send a "typing…" indicator if the channel supports it.
        Implementations may be a no-op.
        """
        ...

    async def send_audio(self, chat_id: str, file_path: Path, *, ptt: bool = False) -> None:
        """Send an audio file to a chat.

        Args:
            chat_id: Target chat identifier.
            file_path: Path to the audio file on disk.
            ptt: If True, send as a voice note (push-to-talk) instead of a regular audio file.
        """
        raise NotImplementedError(f"{type(self).__name__} does not support audio sending")

    async def send_document(
        self,
        chat_id: str,
        file_path: Path,
        *,
        caption: str = "",
        filename: str = "",
    ) -> None:
        """Send a document file to a chat.

        Args:
            chat_id: Target chat identifier.
            file_path: Path to the document file on disk.
            caption: Optional caption shown below the document.
            filename: Display filename for the document (defaults to file_path name).
        """
        raise NotImplementedError(f"{type(self).__name__} does not support document sending")

    @abstractmethod
    async def close(self) -> None:
        """Close channel connections and release resources."""
        ...

    @abstractmethod
    def request_shutdown(self) -> None:
        """Signal the channel to stop accepting new messages."""
        ...


async def _confirm_send(chat_id: str) -> bool:
    """Prompt Y/N for sending a message. Returns True to send.

    After ``SAFE_MODE_MAX_CONFIRM_RETRIES`` invalid inputs the send is
    automatically rejected to prevent an infinite prompt loop from
    misconfigured or automated input sources.
    """
    from src.constants import SAFE_MODE_MAX_CONFIRM_RETRIES

    if not sys.stdin.isatty():
        log.warning(
            "Safe mode requires an interactive terminal — send auto-rejected for chat %s",
            chat_id,
        )
        return False

    for attempt in range(1, SAFE_MODE_MAX_CONFIRM_RETRIES + 1):
        raw = await asyncio.to_thread(input, f"  Send to {chat_id}? [Y/N]: ")
        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        remaining = SAFE_MODE_MAX_CONFIRM_RETRIES - attempt
        if remaining > 0:
            print(f"  Please enter Y or N. ({remaining} attempt{'s' if remaining != 1 else ''} remaining)")
        else:
            print("  Too many invalid inputs. Send rejected.")
            log.warning(
                "Safe-mode confirmation auto-rejected after %d invalid inputs for chat %s",
                SAFE_MODE_MAX_CONFIRM_RETRIES,
                chat_id,
            )
    return False
