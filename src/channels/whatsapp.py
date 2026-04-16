"""
channels/whatsapp.py — WhatsApp channel via neonize (whatsmeow/Go).

Uses neonize (Python ctypes binding for whatsmeow Go library) for direct
WhatsApp Web connection. No Node.js, no subprocess, no HTTP bridge.

First run: displays QR code in terminal for pairing.
Subsequent runs: auto-reconnects using persisted session (SQLite).

Requirements:
  pip install neonize
"""

from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time
from typing import Optional

from src.channels.base import BaseChannel, IncomingMessage, MessageHandler
from src.channels.stealth import (
    cooldown_remaining,
    mark_sent,
    read_delay,
    think_delay,
    type_delay,
    typing_pause_duration,
)
from collections import OrderedDict
from src.config import WhatsAppConfig
from pathlib import Path

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Network resilience helpers
# ─────────────────────────────────────────────────────────────────────────────

_WATCHDOG_INTERVAL = 5  # seconds between connectivity checks
_MAX_SEND_WAIT = 120  # max seconds send() waits for reconnection


def _internet_available() -> bool:
    """Check DNS resolution — quick proxy for internet connectivity."""
    try:
        socket.getaddrinfo("web.whatsapp.com", 443, socket.AF_INET)
        return True
    except (socket.gaierror, OSError):
        return False


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


class NeonizeBackend:
    """
    Native WhatsApp client via neonize (whatsmeow Go library).

    neonize.connect() is blocking — it runs the Go event loop in the
    calling thread. We run it in a daemon thread and bridge callbacks
    to asyncio via run_coroutine_threadsafe + asyncio.Queue.
    """

    def __init__(self, cfg: WhatsAppConfig) -> None:
        self._db_path = cfg.neonize.db_path
        self._client = None
        self._connected = False
        self._connected_at: float = 0.0
        self._ready_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._message_queue: asyncio.Queue[dict] = asyncio.Queue()
        self._connect_thread: Optional[threading.Thread] = None
        # Network outage tracking
        self._disconnect_time: float = 0.0
        self._network_outage: bool = False
        self._watchdog_gen: int = 0
        # Rate-limit whatsmeow's internal reconnect-error spam (once per minute)
        logging.getLogger("whatsmeow.Client").addFilter(_RateLimitFilter())

    @property
    def is_connected(self) -> bool:
        if self._client is None:
            return False
        try:
            return self._client.is_connected
        except Exception:
            return self._connected

    @property
    def is_ready(self) -> bool:
        """True once the connection callback has fired (message loop is live)."""
        return self._ready_event.is_set()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Set up event handlers and start connect() in a background thread."""
        from neonize.client import NewClient
        from neonize.events import (
            ConnectedEv,
            MessageEv,
            DisconnectedEv,
            LoggedOutEv,
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

        @self._client.event(ConnectedEv)
        def _on_connected(_, __):
            self._connected = True
            self._connected_at = time.time()
            self._disconnect_time = 0.0
            self._network_outage = False
            self._ready_event.set()
            log.info("WhatsApp connected")

        @self._client.event(MessageEv)
        def _on_message(client, ev):
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

        @self._client.event(DisconnectedEv)
        def _on_disconnect(_, __):
            self._connected = False
            if self._disconnect_time == 0.0:
                self._disconnect_time = time.time()
            log.warning("WhatsApp disconnected")

        @self._client.event(LoggedOutEv)
        def _on_logged_out(_, __):
            self._connected = False
            log.warning("WhatsApp logged out — need re-pair")

        # connect() is blocking — run in daemon thread
        self._connect_thread = threading.Thread(
            target=self._client.connect, daemon=True
        )
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
        from neonize.utils.jid import build_jid

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
            user, server = _parse_jid(chat_id)
            jid = build_jid(user, server)
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
                log.warning(
                    "Send failed with connection error, reconnecting: %s", send_exc
                )
                try:
                    await self._reconnect()
                    # Wait for background device sync to settle before retrying
                    await asyncio.sleep(5)
                    jid2 = build_jid(user, server)
                    result2 = await asyncio.to_thread(
                        self._client.send_message, jid2, text
                    )
                    msg_id2 = getattr(getattr(result2, "key", None), "ID", "?")
                    mark_sent(chat_id)
                    log.info(
                        "Send succeeded after reconnection (server_msg_id=%s)",
                        msg_id2,
                    )
                    return
                except Exception as retry_exc:
                    log.error("Send failed after reconnection: %s", retry_exc)
                    raise retry_exc
            raise
        finally:
            await self.set_typing(chat_id, composing=False)

    async def set_typing(self, chat_id: str, composing: bool) -> None:
        """Send or clear typing indicator for a chat."""
        if not self.is_connected or self._client is None:
            return
        from neonize.utils.enum import ChatPresence, ChatPresenceMedia
        from neonize.utils.jid import build_jid

        user, server = _parse_jid(chat_id)
        jid = build_jid(user, server)
        state = (
            ChatPresence.CHAT_PRESENCE_COMPOSING
            if composing
            else ChatPresence.CHAT_PRESENCE_PAUSED
        )
        try:
            await asyncio.to_thread(
                self._client.send_chat_presence,
                jid,
                state,
                ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT,
            )
        except Exception as e:
            log.debug("Failed to set typing presence: %s", e)

    async def poll_message(self) -> Optional[dict]:
        """Wait for next message from the queue (async, with timeout)."""
        try:
            return await asyncio.wait_for(self._message_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            return None

    async def _watchdog(self, gen: int) -> None:
        """Background monitor: detect outages, reconnect on internet recovery.

        Uses a generation counter so stale watchdogs from old start() calls
        exit cleanly when a new client is created.
        """
        while self._watchdog_gen == gen and self._client is not None:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            if self.is_connected:
                if self._network_outage:
                    self._network_outage = False
                    log.info("WhatsApp connection recovered")
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
            except Exception as e:
                log.warning("Watchdog reconnect failed: %s", e)

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
            except Exception as e:
                log.debug("Error disconnecting stale client: %s", e)
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

    async def disconnect(self) -> None:
        """Disconnect and clean up."""
        if self._client is not None:
            try:
                await asyncio.to_thread(self._client.disconnect)
            except Exception as e:
                log.warning("Error during disconnect: %s", e)
            self._client = None
        self._connected = False


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp channel
# ─────────────────────────────────────────────────────────────────────────────


class WhatsAppChannel(BaseChannel):
    MAX_QR_WAIT = 120.0

    _CHANNEL_PROMPT = """## WhatsApp Formatting

CRITICAL: You are sending your message to WhatsApp. It does NOT support Markdown.

ALLOWED — Use ONLY these WhatsApp-native formats:
- *bold* → single asterisks
- _italic_ → single underscores
- ~strikethrough~ → single tildes
- ```monospace``` → three backticks

Instead of tables, use plain text with bold labels:
WRONG:
| Period | Temp |
|--------|------|
| Morning | 15°C |

CORRECT:
*☀️ Morning*
• Temp: 15°C
• Wind: 5 km/h

Instead of headers, use bold text or emoji separators:
WRONG: ### Summary
CORRECT: *📋 Summary*

Always format your response for plain-text WhatsApp rendering. Your message must not be recognized as AI-generated by WhatsApp's content filters. Avoid patterns that look like code or AI output. Use natural language formatting and emojis to make it human-like but dont abuse them."""

    def __init__(
        self, cfg: WhatsAppConfig, safe_mode: bool = False, load_history: bool = False
    ) -> None:
        super().__init__(safe_mode=safe_mode, load_history=load_history)
        self._cfg = cfg
        self._backend = NeonizeBackend(cfg)
        self._shutdown_requested = False
        # Per-chat incoming message length tracking (bounded OrderedDict LRU)
        self._last_incoming_len: OrderedDict[str, int] = OrderedDict()
        self._MAX_TRACKED_INCOMING = 500
        # Backpressure: track active handler tasks and consecutive failures
        self._active_tasks: set[asyncio.Task] = set()
        self._consecutive_failures: int = 0
        self._MAX_CONSECUTIVE_FAILURES: int = 10

    def get_channel_prompt(self) -> str | None:
        return self._CHANNEL_PROMPT

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    async def start(self, handler: MessageHandler) -> None:
        loop = asyncio.get_running_loop()

        cli_print("⏳ Connecting to WhatsApp...")
        self._backend.start(loop)

        # Wait for QR scan and connection
        if not self._backend.is_connected:
            cli_print("📱 Scan the QR code displayed above with WhatsApp")
            cli_print("   WhatsApp → Settings → Linked Devices → Link Device")

        start = time.time()
        while not self._backend.is_connected and not self._shutdown_requested:
            if time.time() - start > self.MAX_QR_WAIT:
                log.error("Timeout waiting for WhatsApp connection")
                return
            await asyncio.sleep(2)

        if self._shutdown_requested:
            return

        cli_print("✅ Connected to WhatsApp!")
        log.info("WhatsApp connected, entering message loop")

        # Track user decision for historical messages when load_history is enabled
        _historical_mode = "ask"

        # Message loop — event-driven via queue
        while not self._shutdown_requested:
            msg = await self._backend.poll_message()
            if msg is None:
                continue

            # Build IncomingMessage first (needed by should_process_historical)
            incoming = IncomingMessage(
                message_id=msg["id"],
                chat_id=msg["chat_id"],
                sender_id=msg["sender_id"],
                sender_name=msg["sender_name"],
                text=msg["text"],
                timestamp=msg["timestamp"],
                channel_type="whatsapp",
                fromMe=msg["fromMe"],
                toMe=msg["toMe"],
                is_historical=msg.get("is_historical", False),
                raw=None,
            )

            # Historical message handling
            if incoming.is_historical and not self._load_history:
                log.debug(
                    "Rejecting historical message (load_history=false): %s", msg["id"]
                )
                continue
            if incoming.is_historical and self._load_history:
                # Interactive prompt — only when load_history is explicitly enabled
                if _historical_mode == "reject":
                    log.debug("Rejecting historical message (mode=none): %s", msg["id"])
                    continue
                if _historical_mode == "accept":
                    log.debug("Accepting historical message (mode=all): %s", msg["id"])
                else:
                    choice = await _prompt_historical(msg)
                    if choice == "none":
                        _historical_mode = "reject"
                        continue
                    if choice == "all":
                        _historical_mode = "accept"
                    elif choice == "n":
                        continue

            if self._is_allowed(incoming.sender_id):
                # Backpressure: stop accepting if too many consecutive failures
                if self._consecutive_failures >= self._MAX_CONSECUTIVE_FAILURES:
                    log.error(
                        "Too many consecutive handler failures (%d), dropping message from %s",
                        self._consecutive_failures,
                        incoming.sender_id,
                    )
                    continue

                # Track incoming message length for humanized send timing (LRU)
                self._track_incoming_len(incoming.chat_id, len(incoming.text or ""))
                task = asyncio.create_task(handler(incoming))
                self._active_tasks.add(task)
                task.add_done_callback(self._make_task_callback())
            else:
                log.debug(
                    "Ignored message from %s (not in allowed_numbers)",
                    incoming.sender_id,
                )

    async def close(self) -> None:
        self._shutdown_requested = True
        await self._backend.disconnect()

    def _make_task_callback(self):
        """Create a done callback that tracks failures and provides backpressure."""

        def _on_task_done(task: asyncio.Task) -> None:
            self._active_tasks.discard(task)
            if task.cancelled():
                return
            if exc := task.exception():
                self._consecutive_failures += 1
                log.error(
                    "Handler task failed (%d consecutive): %s",
                    self._consecutive_failures,
                    exc,
                    exc_info=exc,
                )
            else:
                # Reset failure counter on success
                self._consecutive_failures = 0

        return _on_task_done

    def _track_incoming_len(self, chat_id: str, length: int) -> None:
        """Store incoming message length with LRU eviction."""
        if chat_id in self._last_incoming_len:
            self._last_incoming_len.move_to_end(chat_id)
        self._last_incoming_len[chat_id] = length
        # Evict oldest if over cap
        while len(self._last_incoming_len) > self._MAX_TRACKED_INCOMING:
            self._last_incoming_len.popitem(last=False)

    async def _send_message(
        self, chat_id: str, text: str, *, skip_delays: bool = False
    ) -> None:
        incoming_len = self._last_incoming_len.pop(chat_id, 0)
        for chunk in _split_text(text, 4000):
            await self._backend.send(
                chat_id, chunk, incoming_len=incoming_len, skip_delays=skip_delays
            )
            incoming_len = 0  # read delay only on first chunk

    async def send_typing(self, chat_id: str) -> None:
        await self._backend.set_typing(chat_id, composing=True)

    async def send_audio(
        self, chat_id: str, file_path: Path, *, ptt: bool = False
    ) -> None:
        """Send an audio file as a WhatsApp voice note or audio message."""
        if not self._backend.is_connected or self._backend._client is None:
            log.warning("Cannot send audio — WhatsApp not connected")
            return

        # Show brief typing indicator before sending media
        await self._backend.set_typing(chat_id, composing=True)
        try:
            from neonize.utils.jid import build_jid

            user, server = _parse_jid(chat_id)
            jid = build_jid(user, server)
            result = await asyncio.to_thread(
                self._backend._client.send_audio, jid, str(file_path), ptt
            )
            mark_sent(chat_id)
            log.info(
                "Sent audio to %s (ptt=%s, file=%s, msg_id=%s)",
                chat_id,
                ptt,
                file_path.name,
                getattr(result, "message_id", getattr(result, "ID", "?")),
            )
        except Exception as e:
            log.error("Failed to send audio to %s: %s", chat_id, e)
            raise
        finally:
            await self._backend.set_typing(chat_id, composing=False)

    async def send_document(
        self,
        chat_id: str,
        file_path: Path,
        *,
        caption: str = "",
        filename: str = "",
    ) -> None:
        """Send a document file (PDF, etc.) via WhatsApp."""
        if not self._backend.is_connected or self._backend._client is None:
            log.warning("Cannot send document — WhatsApp not connected")
            return

        await self._backend.set_typing(chat_id, composing=True)
        try:
            from neonize.utils.jid import build_jid

            user, server = _parse_jid(chat_id)
            jid = build_jid(user, server)
            fname = filename or file_path.name
            await asyncio.to_thread(
                self._backend._client.send_document,
                jid,
                str(file_path),
                caption=caption or None,
                filename=fname,
            )
            mark_sent(chat_id)
            log.info("Sent document to %s (file=%s)", chat_id, file_path.name)
        except Exception as e:
            log.error("Failed to send document to %s: %s", chat_id, e)
            raise
        finally:
            await self._backend.set_typing(chat_id, composing=False)

    def _is_allowed(self, sender_id: str) -> bool:
        if self._cfg.allowed_numbers:
            # sender_id is already stripped of @server by _extract_message
            return sender_id in self._cfg.allowed_numbers
        # Default-deny: only allow all senders if allow_all is explicitly True
        return self._cfg.allow_all


# ─────────────────────────────────────────────────────────────────────────────
# Message extraction
# ─────────────────────────────────────────────────────────────────────────────


def _extract_message(ev) -> Optional[dict]:
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
    except Exception as e:
        log.warning("Failed to extract message: %s", e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Utilities
# ─────────────────────────────────────────────────────────────────────────────


def _parse_jid(chat_id: str) -> tuple[str, str]:
    """Split a chat_id into (user, server) for JID construction.

    Handles both '@' (from incoming messages) and '_' (from workspace
    directory names) as separators.
    """
    for sep in ("@", "_"):
        if sep in chat_id:
            user, server = chat_id.split(sep, 1)
            return user, server if "." in server else "s.whatsapp.net"
    return chat_id, "s.whatsapp.net"


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


def _is_connection_error(exc: Exception) -> bool:
    """Check if a send error indicates a stale/dead WhatsApp connection."""
    msg = str(exc).lower()
    return any(marker in msg for marker in _CONNECTION_ERROR_MARKERS)


async def _prompt_historical(msg: dict) -> str:
    """Prompt user about a historical message. Returns: y / n / all / none."""
    from rich.console import Console
    from rich.text import Text

    console = Console()
    sender = msg.get("sender_name") or msg.get("sender_id", "?")
    preview = msg.get("text", "")[:80]

    console.print(
        f"\n[yellow]📨 Offline message from [bold]{sender}[/bold]:[/yellow] {preview}"
    )
    console.print("[dim]  Accept this message?[/dim]")

    while True:
        raw = await asyncio.to_thread(input, "  [Y]es / [N]o / [all] / [none]: ")
        choice = raw.strip().lower()
        if choice in ("y", "yes"):
            return "y"
        if choice in ("n", "no"):
            return "n"
        if choice == "all":
            return "all"
        if choice == "none":
            return "none"
        console.print("[dim]  Please enter Y, N, all, or none.[/dim]")


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks without breaking mid-word."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = text.rfind(" ", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip()
    return chunks


def _handle_task_error(task: asyncio.Task) -> None:
    """Log errors from fire-and-forget handler tasks."""
    if task.cancelled():
        return
    if exc := task.exception():
        log.error("Unhandled error in message handler: %s", exc, exc_info=exc)


def cli_print(msg: str) -> None:
    print(msg)
