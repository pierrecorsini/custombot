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
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Coroutine, Optional

if TYPE_CHECKING:
    from src.config import Config
    from src.core.dedup import DeduplicationService
    from src.utils.protocols import Channel

from src.utils.locking import AsyncLock
from src.utils.validation import _CHAT_ID_RE, _validate_chat_id

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
# Canonical definition lives in src/utils/validation.py (_CHAT_ID_RE).
_CHAT_ID_RE  # noqa: B018 — re-exported for local aliasing below.

# Pattern for valid message_id at the message boundary.
# Mirrors _CHAT_ID_RE: alphanumeric, dash, underscore, dot, @.
# Real-world values: WhatsApp "3EB0XXXXXX", CLI UUID "12345678-1234-1234-1234-123456789012".
_MESSAGE_ID_RE = _CHAT_ID_RE

# Pattern for valid sender_id at the message boundary.
# Mirrors _CHAT_ID_RE: alphanumeric, dash, underscore, dot, @.
# Real-world values: WhatsApp phone numbers "1234567890", CLI chat IDs "cli-abc123".
_SENDER_ID_RE = _CHAT_ID_RE


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
        raise TypeError(f"IncomingMessage.sender_id must be a str, got {type(sender_id).__name__}")
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


# Characters allowed in sender_name.
# Broader than the ID regex: allows spaces, commas, Unicode letters,
# and common punctuation that appear in real display names.
# Rejects path separators and other dangerous printable chars.
# Note: control characters (\x00-\x1f, \x7f) are stripped before this check.
_SENDER_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f/\\<>\"|?*]+$")

# Control characters stripped from sender_name before validation.
# Covers null, tabs, newlines, carriage returns, ANSI escapes, DEL, etc.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_sender_name(sender_name: object, *, max_length: int = 200) -> str:
    """Sanitize and validate ``sender_name`` at the IncomingMessage boundary.

    Strips non-printable characters (control chars \\x00-\\x1f, \\x7f) and
    truncates to ``max_length``.  After sanitization, still rejects names
    containing dangerous printable characters (path separators, shell
    metacharacters).  Empty string is allowed — some channels omit display
    names.

    Returns:
        The sanitized sender_name string.

    Raises:
        TypeError: If ``sender_name`` is not a string.
        ValueError: If the sanitized ``sender_name`` contains unsafe
            printable characters.
    """
    from src.constants import MAX_SENDER_NAME_LENGTH

    if not isinstance(sender_name, str):
        raise TypeError(
            f"IncomingMessage.sender_name must be a str, got {type(sender_name).__name__}"
        )
    # Strip control characters (ANSI escapes, null bytes, tabs, newlines, etc.)
    sanitized = _CONTROL_CHARS_RE.sub("", sender_name)
    # Truncate to maximum length
    effective_max = max_length or MAX_SENDER_NAME_LENGTH
    sanitized = sanitized[:effective_max]
    # Reject dangerous printable characters (path separators, shell metacharacters).
    if sanitized and not _SENDER_NAME_RE.match(sanitized):
        raise ValueError(
            f"IncomingMessage.sender_name contains unsafe characters: {sanitized!r}. "
            "Path separators and shell metacharacters are not allowed."
        )
    return sanitized


def _validate_timestamp(timestamp: object) -> None:
    """Validate ``timestamp`` at the IncomingMessage boundary.

    Rejects non-numeric types, NaN, Infinity, and values outside the
    plausible Unix-epoch range (1970 through 2100).

    Raises:
        TypeError: If ``timestamp`` is not a number.
        ValueError: If ``timestamp`` is NaN, Inf, or outside the valid range.
    """
    from src.constants import TIMESTAMP_MAX, TIMESTAMP_MIN

    if not isinstance(timestamp, (int, float)):
        raise TypeError(
            f"IncomingMessage.timestamp must be a number, got {type(timestamp).__name__}"
        )
    if isinstance(timestamp, float) and (timestamp != timestamp):  # NaN check
        raise ValueError("IncomingMessage.timestamp must not be NaN")
    if isinstance(timestamp, float):
        import math

        if math.isinf(timestamp):
            raise ValueError("IncomingMessage.timestamp must not be Inf")
    if timestamp < TIMESTAMP_MIN:
        raise ValueError(f"IncomingMessage.timestamp is too small ({timestamp} < {TIMESTAMP_MIN})")
    if timestamp > TIMESTAMP_MAX:
        raise ValueError(f"IncomingMessage.timestamp is too large ({timestamp} > {TIMESTAMP_MAX})")


def _validate_correlation_id(correlation_id: object, *, max_length: int = 200) -> None:
    """Validate ``correlation_id`` at the IncomingMessage boundary.

    Only called when ``correlation_id`` is not None.  Enforces type,
    length, and character-safety constraints so tracing/logging backends
    never receive crafted payloads.

    Raises:
        TypeError: If ``correlation_id`` is not a string.
        ValueError: If ``correlation_id`` is empty, too long, or contains
            unsafe characters.
    """
    from src.constants import MAX_CORRELATION_ID_LENGTH

    if not isinstance(correlation_id, str):
        raise TypeError(
            f"IncomingMessage.correlation_id must be a str, got {type(correlation_id).__name__}"
        )
    if not correlation_id:
        raise ValueError("IncomingMessage.correlation_id must not be empty")
    effective_max = max_length or MAX_CORRELATION_ID_LENGTH
    if len(correlation_id) > effective_max:
        raise ValueError(
            f"IncomingMessage.correlation_id exceeds maximum length "
            f"({len(correlation_id)} > {effective_max}): {correlation_id[:40]!r}..."
        )
    if not _CHAT_ID_RE.match(correlation_id):
        raise ValueError(
            f"IncomingMessage.correlation_id contains invalid characters: {correlation_id!r}. "
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
    raw: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        # Defense-in-depth: validate identifiers before they reach any filesystem op.
        _validate_chat_id(self.chat_id)
        _validate_message_id(self.message_id)
        _validate_sender_id(self.sender_id)
        # Sanitize sender_name: strip control chars + truncate (frozen → setattr).
        sanitized_name = _validate_sender_name(self.sender_name)
        if sanitized_name != self.sender_name:
            object.__setattr__(self, "sender_name", sanitized_name)
        _validate_timestamp(self.timestamp)
        if self.correlation_id is not None:
            _validate_correlation_id(self.correlation_id)

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
MessageHandler = Callable[[IncomingMessage], Coroutine[Any, Any, None]]

# Type alias for the media-sending callback injected into skills.
# Callable(kind: "audio" | "document", path: Path, caption: str) -> Awaitable[None]
SendMediaCallback = Callable[[str, Path, str], Awaitable[None]]


# Module-level lock for safe-mode serialisation.  AsyncLock defers the
# actual asyncio.Lock creation until first acquire(), so it is safe to
# create at import time — see src.utils.locking policy.
_safe_mode_lock = AsyncLock()


def _redact_chat_id(chat_id: str) -> str:
    """Redact ``chat_id`` (potentially a phone number) for terminal display.

    Shows first 4 and last 4 characters for long IDs, preserving any
    ``@suffix`` portion intact.  Short local parts are truncated to the
    first 4 characters plus an ellipsis.
    """
    if "@" in chat_id:
        local, _, suffix = chat_id.partition("@")
        if len(local) <= 8:
            return f"{local[:4]}...@{suffix}"
        return f"{local[:4]}...{local[-4:]}@{suffix}"
    if len(chat_id) <= 8:
        return chat_id[:4] + "..."
    return chat_id[:4] + "..." + chat_id[-4:]


class BaseChannel(ABC):
    """
    Abstract async channel with optional safe mode.

    Safe mode (--safe) intercepts every outgoing message and prompts
    the user for Y/N confirmation before sending. Channels only need
    to implement _send_message(); send_message() is handled here.

    A module-level AsyncLock (from src.utils.locking) serialises all
    safe-mode prompts so that parallel message processing never
    interleaves preview output or Y/N prompts on the terminal.
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
            display_id = _redact_chat_id(chat_id)
            async with _safe_mode_lock:
                print(f"\n📤 Outgoing to {display_id}:")
                print(f"   {preview}")
                if not await _confirm_send(chat_id, display_id):
                    log.info("Send cancelled by user (safe mode): %s", chat_id)
                    return
            log.debug("Full chat_id for safe-mode send: %s", chat_id)
        await self._send_message(chat_id, text, skip_delays=skip_delays)

    async def send_and_track(
        self,
        chat_id: str,
        text: str,
        *,
        dedup: DeduplicationService | None = None,
        skip_delays: bool = False,
    ) -> None:
        """Send a message, record outbound dedup, and emit a ``response_sent`` event.

        Centralizes the send → dedup → event pipeline so that *all* outbound
        paths (normal responses, error replies, scheduled replies, rate-limit
        warnings) are tracked consistently.  Callers that only need persistence
        without an actual channel send should use
        :func:`src.bot.response_delivery.send_to_chat` directly with
        ``channel=None``.

        Args:
            chat_id: Target chat identifier.
            text: Message body.
            dedup: Optional deduplication service for outbound tracking.
            skip_delays: Bypass human-like timing delays (for scheduled tasks).
        """
        from src.core.errors import NonCriticalCategory, log_noncritical
        from src.core.event_bus import Event, get_event_bus
        from src.logging import get_correlation_id

        try:
            await self.send_message(chat_id, text, skip_delays=skip_delays)
        except Exception:
            # send_message already handles safe-mode; this catch is for
            # unexpected transport errors so that dedup + event still fire.
            log.warning(
                "send_and_track: send_message failed for chat %s",
                chat_id,
                exc_info=True,
            )

        if dedup:
            dedup.record_outbound(chat_id, text)

        try:
            await get_event_bus().emit(
                Event(
                    name="response_sent",
                    data={"chat_id": chat_id, "response_length": len(text)},
                    source="BaseChannel.send_and_track",
                    correlation_id=get_correlation_id(),
                )
            )
        except Exception:
            log_noncritical(
                NonCriticalCategory.EVENT_EMISSION,
                f"Failed to emit response_sent event for chat {chat_id}",
                logger=log,
            )

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


async def _confirm_send(chat_id: str, display_id: str) -> bool:
    """Prompt Y/N for sending a message. Returns True to send.

    Args:
        chat_id: Full chat identifier (used for structured logging).
        display_id: Redacted identifier for terminal display.

    After ``SAFE_MODE_MAX_CONFIRM_RETRIES`` invalid inputs the send is
    automatically rejected to prevent an infinite prompt loop from
    misconfigured or automated input sources.
    """
    from src.constants import SAFE_MODE_CONFIRM_TIMEOUT, SAFE_MODE_MAX_CONFIRM_RETRIES

    if not sys.stdin.isatty():
        log.warning(
            "Safe mode requires an interactive terminal — send auto-rejected for chat %s",
            chat_id,
        )
        return False

    for attempt in range(1, SAFE_MODE_MAX_CONFIRM_RETRIES + 1):
        try:
            raw = await asyncio.wait_for(
                asyncio.to_thread(input, f"  Send to {display_id}? [Y/N]: "),
                timeout=SAFE_MODE_CONFIRM_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print("  Confirmation timed out. Send rejected.")
            log.warning(
                "Safe-mode confirmation timed out after %.0fs for chat %s",
                SAFE_MODE_CONFIRM_TIMEOUT,
                chat_id,
            )
            return False
        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            return True
        if choice in ("n", "no"):
            return False
        remaining = SAFE_MODE_MAX_CONFIRM_RETRIES - attempt
        if remaining > 0:
            print(
                f"  Please enter Y or N. ({remaining} attempt{'s' if remaining != 1 else ''} remaining)"
            )
        else:
            print("  Too many invalid inputs. Send rejected.")
            log.warning(
                "Safe-mode confirmation auto-rejected after %d invalid inputs for chat %s",
                SAFE_MODE_MAX_CONFIRM_RETRIES,
                chat_id,
            )
    return False
