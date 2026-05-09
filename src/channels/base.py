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

from src.channels.message_validator import _validator
from src.core.dedup import NullDedupService, outbound_key
from src.channels.registry import ChannelState

log = logging.getLogger(__name__)

# ── Redaction constants ──────────────────────────────────────────────────
REDACT_PREVIEW_LENGTH: int = 40
REDACT_ID_LENGTH: int = 12
REDACT_SHORT_THRESHOLD: int = 8
REDACT_VISIBLE_CHARS: int = 4


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
        _validator.validate(self)

    def __repr__(self) -> str:
        text_preview = self.text[:REDACT_PREVIEW_LENGTH] + "..." if len(self.text) > REDACT_PREVIEW_LENGTH else self.text
        return (
            f"IncomingMessage(id={self.message_id[:REDACT_ID_LENGTH]!r}..., chat={self.chat_id!r}, "
            f"sender={self.sender_name!r}, text={text_preview!r}, channel={self.channel_type!r})"
        )


# Type alias for the callback the bot registers
MessageHandler = Callable[[IncomingMessage], Coroutine[Any, Any, None]]

# Type alias for the media-sending callback injected into skills.
# Callable(kind: "audio" | "document", path: Path, caption: str) -> Awaitable[None]
SendMediaCallback = Callable[[str, Path, str], Awaitable[None]]


def is_group_chat(chat_id: str) -> bool:
    """Return True if *chat_id* identifies a group conversation.

    Group chat identifiers carry a platform-specific suffix (e.g. ``@g.us``
    for WhatsApp groups).  Detecting groups is used to suppress noisy
    rate-limit replies that would spam all group members.
    """
    return chat_id.endswith("@g.us")


def _redact_chat_id(chat_id: str) -> str:
    """Redact ``chat_id`` (potentially a phone number) for terminal display.

    Shows first 4 and last 4 characters for long IDs, preserving any
    ``@suffix`` portion intact.  Short local parts are truncated to the
    first 4 characters plus an ellipsis.
    """
    if "@" in chat_id:
        local, _, suffix = chat_id.partition("@")
        if len(local) <= REDACT_SHORT_THRESHOLD:
            return f"{local[:REDACT_VISIBLE_CHARS]}...@{suffix}"
        return f"{local[:REDACT_VISIBLE_CHARS]}...{local[-REDACT_VISIBLE_CHARS:]}@{suffix}"
    if len(chat_id) <= REDACT_SHORT_THRESHOLD:
        return chat_id[:REDACT_VISIBLE_CHARS] + "..."
    return chat_id[:REDACT_VISIBLE_CHARS] + "..." + chat_id[-REDACT_VISIBLE_CHARS:]


class BaseChannel(ABC):
    """
    Abstract async channel with optional safe mode.

    Safe mode (--safe) intercepts every outgoing message and prompts
    the user for Y/N confirmation before sending. Channels only need
    to implement _send_message(); send_message() is handled here.

    An instance-level AsyncLock (from src.utils.locking) serialises all
    safe-mode prompts for this channel so that parallel message processing
    never interleaves preview output or Y/N prompts on the terminal.
    Each channel instance has its own lock, avoiding shared mutable state.
    """

    def __init__(self, safe_mode: bool = False, load_history: bool = False) -> None:
        self._safe_mode = safe_mode
        self._load_history = load_history
        self._safe_mode_lock = AsyncLock()
        self._connected_event: asyncio.Event = asyncio.Event()
        self._state: ChannelState = ChannelState.DISCONNECTED

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
        self._state = ChannelState.CONNECTED

    @property
    def state(self) -> ChannelState:
        """Return the current channel lifecycle state."""
        return self._state

    async def on_connect(self) -> None:
        """Lifecycle hook called after successful connection.

        Override in subclasses to perform post-connection setup
        (e.g. loading state, subscribing to topics).
        """
        pass

    async def on_disconnect(self) -> None:
        """Lifecycle hook called after disconnection.

        Override in subclasses to perform cleanup
        (e.g. flushing buffers, cancelling timers).
        """
        pass

    async def on_error(self, error: Exception) -> None:
        """Lifecycle hook called when a channel error occurs.

        Override in subclasses to handle errors
        (e.g. logging, metrics, reconnection logic).
        """
        self._state = ChannelState.ERROR
        log.warning("Channel error in %s: %s", type(self).__name__, error)

    async def on_message(self, msg: IncomingMessage) -> None:
        """Lifecycle hook called for each incoming message.

        Override in subclasses to add per-message processing
        (e.g. metrics, transformation, filtering).
        """
        pass

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
            async with self._safe_mode_lock:
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
        dedup: DeduplicationService = NullDedupService(),
        skip_delays: bool = False,
        dedup_key: str | None = None,
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
            dedup_key: Pre-computed xxh64 hash key from
                :meth:`~src.core.dedup.DeduplicationService.check_outbound_with_key`.
                When provided, :meth:`record_outbound_keyed` is used directly.
                When absent, the hash is computed inline via :func:`outbound_key`
                so no redundant xxh64 computation occurs.
        """
        from src.core.errors import NonCriticalCategory, log_noncritical
        from src.core.event_bus import Event, get_event_bus
        from src.logging import get_correlation_id

        try:
            await self.send_message(chat_id, text, skip_delays=skip_delays)
        except Exception:
            log.warning(
                "send_and_track: send_message failed for chat %s",
                chat_id,
                exc_info=True,
            )
            return

        if dedup_key is not None:
            dedup.record_outbound_keyed(dedup_key)
        else:
            # Compute hash inline to avoid double-hash via record_outbound().
            dedup.record_outbound_keyed(outbound_key(chat_id, text))

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
