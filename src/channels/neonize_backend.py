"""
channels/neonize_backend.py — WhatsApp transport layer via neonize (whatsmeow/Go).

Manages the raw neonize client lifecycle: connection, reconnection,
message extraction, typing indicators, and network resilience.

Uses neonize (Python ctypes binding for whatsmeow Go library) for direct
WhatsApp Web connection. No Node.js, no subprocess, no HTTP bridge.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional

from src.channels.stealth import (
    cooldown_remaining,
    mark_sent,
    read_delay,
    think_delay,
    type_delay,
    typing_pause_duration,
)
from src.config import WhatsAppConfig
from src.ui.cli_output import cli as cli_output
from src.utils import LRUDict
from src.utils.retry import BACKOFF_MULTIPLIER, calculate_delay_with_jitter

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_WATCHDOG_INTERVAL = 5  # seconds between connectivity checks
_MAX_SEND_WAIT = 120  # max seconds send() waits for reconnection
_MAX_RECONNECT_DELAY = 60  # max backoff between watchdog reconnection attempts

_CONNECTION_ERROR_MARKERS = (
    "usync",
    "device list",
    "timed out",
    "timeout",
    "not connected",
    "connection reset",
    "broken pipe",
    "no such session",
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _build_jid(chat_id: str, agent: Any) -> Any:
    """Deferred import wrapper for neonize build_jid."""
    from neonize.utils.jid import build_jid

    return build_jid(chat_id, agent)


def _internet_available() -> bool:
    """Check DNS resolution — quick proxy for internet connectivity."""
    try:
        socket.getaddrinfo("web.whatsapp.com", 443, socket.AF_INET)
        return True
    except (socket.gaierror, OSError):
        return False


def _is_connection_error(exc: Exception) -> bool:
    """Check if a send error indicates a stale/dead WhatsApp connection."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONNECTION_ERROR_MARKERS)


def _parse_jid(chat_id: str) -> tuple[str, str]:
    """Split a chat_id into (user, server) for JID construction.

    Prefers the '@' separator (from incoming message JIDs). Falls back to '_'
    (from workspace directory names after sanitization).

    If the chat_id looks sanitized (contains '_at_' but no '@'), attempts to
    read the original chat_id from the workspace .chat_id metadata file.
    """
    from pathlib import Path

    # Prefer direct '@' separator from original JIDs
    if "@" in chat_id:
        user, server = chat_id.split("@", 1)
        return user, server if "." in server else "s.whatsapp.net"

    # If this looks like a sanitized chat_id, try reading the original
    if "_at_" in chat_id:
        try:
            from src.constants import WORKSPACE_DIR
            from src.utils.path import sanitize_path_component

            meta_path = (
                Path(WORKSPACE_DIR)
                / "whatsapp_data"
                / sanitize_path_component(chat_id)
                / ".chat_id"
            )
            if meta_path.exists():
                original = meta_path.read_text(encoding="utf-8").strip()
                if "@" in original:
                    user, server = original.split("@", 1)
                    return user, server if "." in server else "s.whatsapp.net"
        except (OSError, ValueError) as exc:
            log.debug("Could not read .chat_id metadata for %s: %s", chat_id, exc)

    # Fallback: try '_' as separator
    if "_" in chat_id:
        user, server = chat_id.split("_", 1)
        return user, server if "." in server else "s.whatsapp.net"

    return chat_id, "s.whatsapp.net"


def _extract_message(ev: Any) -> Optional[dict[str, Any]]:
    """Extract a normalized message dict from a neonize MessageEv."""
    try:
        info = ev.Info
        source = info.MessageSource
        msg = ev.Message

        text = (
            getattr(msg, "conversation", None)
            or getattr(getattr(msg, "extendedTextMessage", None), "text", None)
            or ""
        )
        if not text:
            return None

        chat_jid = source.Chat  # e.g. "1234567890@s.whatsapp.net" (protobuf JID)
        sender_jid = source.Sender

        # Extract user portion (number) from JID
        chat_str = f"{chat_jid.User}@{chat_jid.Server}" if chat_jid.User else ""
        sender_str = f"{sender_jid.User}@{sender_jid.Server}" if sender_jid.User else ""
        sender_id = sender_jid.User or chat_jid.User or ""

        if not chat_str or not sender_id:
            log.warning(
                "Dropping message with empty identity: chat=%r sender=%r",
                chat_str,
                sender_id,
            )
            return None

        is_group = source.IsGroup
        from_me = bool(source.IsFromMe)
        log.debug(
            "Extracted message: chat=%s sender=%s is_group=%s fromMe=%s",
            chat_str,
            sender_id,
            is_group,
            from_me,
        )
        return {
            "id": info.ID,
            "chat_id": chat_str,
            "sender_id": sender_id,
            "sender_name": info.Pushname or "",
            "text": text,
            "timestamp": info.Timestamp,
            "fromMe": from_me,
            "toMe": not is_group and (not from_me or sender_str == chat_str),
        }
    except Exception as exc:
        log.warning("Failed to extract message: %s", exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Rate-limit filter
# ─────────────────────────────────────────────────────────────────────────────


class _RateLimitFilter(logging.Filter):
    """Suppress repeated log messages within a time window.

    Applied to the whatsmeow logger to prevent error spam during outages.
    Each unique message (first 100 chars) is allowed through once per interval.
    Evicts stale entries when the dict exceeds 200 keys to prevent unbounded growth.
    """

    _MAX_ENTRIES = 200

    def __init__(self, seconds: int = 60) -> None:
        super().__init__()
        self._seconds = seconds
        self._last: dict[str, float] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = record.getMessage()[:100]
        now = time.time()
        # Evict stale entries periodically to prevent unbounded growth
        if len(self._last) > self._MAX_ENTRIES:
            cutoff = now - self._seconds
            self._last = {k: v for k, v in self._last.items() if v >= cutoff}
        if now - self._last.get(key, 0) < self._seconds:
            return False
        self._last[key] = now
        return True


# ─────────────────────────────────────────────────────────────────────────────
# NeonizeBackend
# ─────────────────────────────────────────────────────────────────────────────


class NeonizeBackend:
    """
    Native WhatsApp client via neonize (whatsmeow Go library).

    neonize.connect() is blocking — it runs the Go event loop in the
    calling thread. We run it in a daemon thread and bridge callbacks
    to asyncio via run_coroutine_threadsafe + asyncio.Queue.
    """

    def __init__(self, cfg: WhatsAppConfig) -> None:
        self._db_path = cfg.neonize.db_path
        self._client: Any = None
        self._connected = False
        self._connected_at: float = 0.0
        self._ready_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._message_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._connect_thread: Optional[threading.Thread] = None
        # Network outage tracking
        self._disconnect_time: float = 0.0
        self._network_outage: bool = False
        self._watchdog_gen: int = 0
        # Reconnection backoff state
        self._reconnect_delay: float = _WATCHDOG_INTERVAL
        # Bounded LRU cache for resolved JIDs — avoids redundant string
        # parsing and .chat_id file reads on every send/typing call.
        self._jid_cache: LRUDict = LRUDict(max_size=500)
        # Rate-limit whatsmeow's internal reconnect-error spam (once per minute)
        logging.getLogger("whatsmeow.Client").addFilter(_RateLimitFilter())

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        try:
            connected: bool = bool(self._client.is_connected)
            return connected
        except Exception as exc:
            log.debug("is_connected check failed: %s", exc)
            return self._connected

    @property
    def is_ready(self) -> bool:
        """True once the connection callback has fired (message loop is live)."""
        return self._ready_event.is_set()

    def _resolve_jid(self, chat_id: str) -> Any:
        """Resolve a chat_id string to a neonize JID object (cached).

        Avoids redundant _parse_jid string parsing and potential .chat_id
        file reads on every send/typing call by caching resolved JIDs in
        a bounded LRU dict.  The cache is invalidated on reconnection.
        """
        cached = self._jid_cache.get(chat_id)
        if cached is not None:
            return cached
        user, server = _parse_jid(chat_id)
        jid = _build_jid(user, server)
        self._jid_cache[chat_id] = jid
        return jid

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set up event handlers and start connect() in a background thread."""
        from neonize.client import NewClient
        from neonize.events import (
            ConnectedEv,
            DisconnectedEv,
            LoggedOutEv,
            MessageEv,
        )
        from neonize.proto.waCompanionReg.WAWebProtobufsCompanionReg_pb2 import (
            DeviceProps,
        )

        self._loop = loop

        # Mimic a real WhatsApp Desktop linked device
        props = DeviceProps()
        props.os = "Windows"
        props.platformType = DeviceProps.DESKTOP
        props.requireFullSync = False

        # Realistic HistorySyncConfig — matches what WhatsApp Desktop actually sends
        hsc = props.historySyncConfig
        hsc.fullSyncDaysLimit = 30
        hsc.fullSyncSizeMbLimit = 100
        hsc.storageQuotaMb = 100
        hsc.recentSyncDaysLimit = 7
        hsc.supportCallLogHistory = False
        hsc.supportBotUserAgentChatHistory = False
        hsc.supportCagReactionsAndPolls = True
        hsc.supportBizHostedMsg = True
        hsc.supportHostedGroupMsg = True
        hsc.supportRecentSyncChunkMessageCountTuning = True
        hsc.supportAddOnHistorySyncMigration = True
        hsc.supportMessageAssociation = False

        self._client = NewClient(self._db_path, props=props)

        @self._client.event(ConnectedEv)  # type: ignore[untyped-decorator]
        def _on_connected(_: Any, __: Any) -> None:
            self._connected = True
            self._connected_at = time.time()
            self._disconnect_time = 0.0
            self._network_outage = False
            self._ready_event.set()
            log.info("WhatsApp connected")

        @self._client.event(MessageEv)  # type: ignore[untyped-decorator]
        def _on_message(client: Any, ev: Any) -> None:
            msg = _extract_message(ev)
            if msg is None or self._loop is None:
                return
            # A message is historical if it arrived before the connected callback fired
            if not self._ready_event.is_set():
                msg["is_historical"] = True
            elif self._connected_at > 0 and msg["timestamp"] < self._connected_at:
                msg["is_historical"] = True
            else:
                msg["is_historical"] = False
            asyncio.run_coroutine_threadsafe(self._message_queue.put(msg), self._loop)

        @self._client.event(DisconnectedEv)  # type: ignore[untyped-decorator]
        def _on_disconnect(_: Any, __: Any) -> None:
            self._connected = False
            if self._disconnect_time == 0.0:
                self._disconnect_time = time.time()
            log.warning("WhatsApp disconnected")

        @self._client.event(LoggedOutEv)  # type: ignore[untyped-decorator]
        def _on_logged_out(_: Any, __: Any) -> None:
            self._connected = False
            log.warning("WhatsApp logged out — need re-pair")

        # connect() is blocking — run in daemon thread
        self._connect_thread = threading.Thread(target=self._client.connect, daemon=True)
        self._connect_thread.start()

        # Start connection watchdog (generation-based to avoid stale tasks)
        self._watchdog_gen += 1
        asyncio.run_coroutine_threadsafe(self._watchdog(self._watchdog_gen), loop)

    async def send(
        self,
        chat_id: str,
        text: str,
        incoming_len: int = 0,
        skip_delays: bool = False,
    ) -> None:
        """Send a text message with human-like timing and per-chat cooldown.

        Args:
            chat_id: Target chat identifier.
            text: Message body.
            incoming_len: Length of the incoming message (for read delay simulation).
            skip_delays: When True, skip human-like delays — used for scheduled
                task delivery where no conversation context exists.
        """
        if not self.is_connected or self._client is None:
            log.info("WhatsApp disconnected, waiting for reconnection...")
            if not await self._wait_for_connection(_MAX_SEND_WAIT):
                raise RuntimeError("Not connected to WhatsApp (internet may be down)")
        if not skip_delays:
            # Respect per-chat cooldown
            cd = cooldown_remaining(chat_id)
            if cd > 0:
                await asyncio.sleep(cd)

            # Phase 1: Read delay (simulate reading the incoming message)
            if incoming_len > 0:
                await asyncio.sleep(read_delay(incoming_len))

            # Phase 2: Show typing + think delay
            await self.set_typing(chat_id, composing=True)
            await asyncio.sleep(think_delay())

            # Phase 3: Occasional mid-typing pause (human behavior)
            pause = typing_pause_duration()
            if pause > 0:
                await asyncio.sleep(pause / 2)
                await self.set_typing(chat_id, composing=False)
                await asyncio.sleep(pause / 2)
                await self.set_typing(chat_id, composing=True)

            # Phase 4: Type delay (proportional to response length)
            await asyncio.sleep(type_delay(len(text)))
        else:
            # Minimal typing indicator for scheduled messages
            await self.set_typing(chat_id, composing=True)

        try:
            jid = self._resolve_jid(chat_id)
            result = await asyncio.to_thread(self._client.send_message, jid, text)
            # Log server confirmation to distinguish "queued" from "delivered"
            msg_id = getattr(getattr(result, "key", None), "ID", "?")
            log.info(
                "Message sent to %s (server_msg_id=%s, chat_id=%s)",
                chat_id,
                msg_id,
                chat_id,
            )
            mark_sent(chat_id)
        except Exception as send_exc:
            # Connection stale (usync timeout, device list failure, etc.) → reconnect & retry once
            if _is_connection_error(send_exc):
                log.warning("Send failed with connection error, reconnecting: %s", send_exc)
                try:
                    await self._reconnect()
                    # Wait for background device sync to settle before retrying
                    await asyncio.sleep(5)
                    # Invalidate JID cache after reconnection (client changed)
                    self._jid_cache.pop(chat_id, None)
                    jid2 = self._resolve_jid(chat_id)
                    result2 = await asyncio.to_thread(self._client.send_message, jid2, text)
                    msg_id2 = getattr(getattr(result2, "key", None), "ID", "?")
                    mark_sent(chat_id)
                    log.info(
                        "Send succeeded after reconnection (server_msg_id=%s)",
                        msg_id2,
                    )
                    return
                except Exception as retry_exc:
                    log.error("Send failed after reconnection: %s", retry_exc)
                    # Defensive clear — the outer finally also clears typing,
                    # but an explicit clear here makes the intent obvious and
                    # guards against future refactoring of the finally block.
                    await self.set_typing(chat_id, composing=False)
                    raise retry_exc
            raise
        finally:
            await self.set_typing(chat_id, composing=False)

    async def set_typing(self, chat_id: str, composing: bool) -> None:
        """Send or clear typing indicator for a chat."""
        if not self.is_connected or self._client is None:
            return
        from neonize.utils.enum import ChatPresence, ChatPresenceMedia

        jid = self._resolve_jid(chat_id)
        state = (
            ChatPresence.CHAT_PRESENCE_COMPOSING if composing else ChatPresence.CHAT_PRESENCE_PAUSED
        )
        try:
            await asyncio.to_thread(
                self._client.send_chat_presence,
                jid,
                state,
                ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
            )
        except Exception as exc:
            log.debug("Failed to set typing presence: %s", exc)

    async def poll_message(self) -> Optional[dict[str, Any]]:
        """Wait for next message from the queue (async, with timeout)."""
        try:
            return await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    async def _watchdog(self, gen: int) -> None:
        """Background monitor: detect outages, reconnect on internet recovery.

        Uses a generation counter so stale watchdogs from old start() calls
        exit cleanly when a new client is created.

        Reconnection attempts use exponential backoff with jitter to avoid
        overwhelming the WhatsApp server during sustained outages. The backoff
        resets on successful reconnection or when already connected.
        """
        while self._watchdog_gen == gen and self._client is not None:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if self.is_connected:
                if self._network_outage:
                    self._network_outage = False
                    log.info("WhatsApp connection recovered")
                # Reset backoff on confirmed connectivity
                self._reconnect_delay = _WATCHDOG_INTERVAL
                continue

            if self._disconnect_time == 0.0:
                continue

            has_internet = await asyncio.to_thread(_internet_available)
            if not has_internet:
                if not self._network_outage:
                    self._network_outage = True
                    log.warning(
                        "Internet unreachable — WhatsApp reconnecting "
                        "automatically when connectivity returns"
                    )
                continue

            # Internet is back but still disconnected — force reconnect
            if self._network_outage:
                self._network_outage = False
                log.info("Internet restored, reconnecting WhatsApp...")
            try:
                await self._reconnect()
                # Success — reset backoff to base interval
                self._reconnect_delay = _WATCHDOG_INTERVAL
            except Exception as exc:
                log.warning("Watchdog reconnect failed: %s", exc)
                # Exponential backoff with jitter, capped at max
                delay_with_jitter = calculate_delay_with_jitter(self._reconnect_delay)
                log.info(
                    "Next reconnect attempt in %.1fs (backoff)",
                    delay_with_jitter,
                )
                await asyncio.sleep(delay_with_jitter)
                self._reconnect_delay = min(
                    self._reconnect_delay * BACKOFF_MULTIPLIER,
                    _MAX_RECONNECT_DELAY,
                )

    async def _wait_for_connection(self, timeout: float) -> bool:
        """Wait until connected. Returns True if connection restored in time."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_connected:
                return True
            await asyncio.sleep(1)
        return self.is_connected

    async def _reconnect(self, timeout: float = 30.0) -> None:
        """Disconnect stale client and reconnect using the same session DB.

        neonize/whatsmeow keeps a persistent SQLite session — reconnecting
        reuses it without requiring another QR scan.
        """
        log.info("Reconnecting WhatsApp client...")

        # Tear down old client
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception as exc:
                log.debug("Error disconnecting stale client: %s", exc)
            self._client = None

        self._connected = False
        self._ready_event.clear()

        # Reconnect with the same event loop — start() reuses self._loop
        assert self._loop is not None, "Cannot reconnect without event loop"
        self.start(self._loop)

        # Wait for connection (run blocking Event.wait in thread pool)
        connected = await asyncio.to_thread(self._ready_event.wait, timeout)
        if not connected:
            raise RuntimeError("WhatsApp reconnection timed out")

        log.info("WhatsApp reconnected successfully")

    async def send_audio(self, chat_id: str, file_path: str, ptt: bool = False) -> Any:
        """Send an audio file via WhatsApp.

        Args:
            chat_id: Target chat identifier.
            file_path: Path to the audio file.
            ptt: If True, send as a push-to-talk voice note.

        Returns:
            The neonize send result (with message_id attribute).
        """
        if not self.is_connected or self._client is None:
            raise RuntimeError("WhatsApp not connected")
        jid = self._resolve_jid(chat_id)
        return await asyncio.to_thread(self._client.send_audio, jid, file_path, ptt)

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: str | None = None,
        filename: str | None = None,
    ) -> None:
        """Send a document file (PDF, etc.) via WhatsApp.

        Args:
            chat_id: Target chat identifier.
            file_path: Path to the document file.
            caption: Optional caption text.
            filename: Optional display filename (defaults to file_path basename).
        """
        if not self.is_connected or self._client is None:
            raise RuntimeError("WhatsApp not connected")
        jid = self._resolve_jid(chat_id)
        await asyncio.to_thread(
            self._client.send_document,
            jid,
            file_path,
            caption=caption,
            filename=filename,
        )

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception as exc:
                log.warning("Error during disconnect: %s", exc)
            self._client = None
        self._connected = False
