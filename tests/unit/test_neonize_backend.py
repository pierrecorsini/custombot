"""
test_neonize_backend.py — Tests for NeonizeBackend connection lifecycle.

Covers:
- Watchdog detects disconnection and triggers reconnect
- Watchdog exits when generation increments (stale task)
- _wait_for_connection() returns True when connected in time
- _wait_for_connection() returns False on timeout
- Message queue bridge from neonize thread callbacks to asyncio
- _extract_message() with valid and invalid inputs
- _is_connection_error() classification
- _parse_jid() with various chat_id formats
- send() waits for reconnection then sends
- send() raises after reconnection timeout
- disconnect() cleans up client state
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.neonize_backend import (
    NeonizeBackend,
    _CONNECTION_ERROR_MARKERS,
    _MAX_RECONNECT_DELAY,
    _WATCHDOG_INTERVAL,
    _extract_message,
    _is_connection_error,
    _parse_jid,
)
from src.config import NeonizeConfig, WhatsAppConfig
from src.utils.retry import BACKOFF_MULTIPLIER


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_backend() -> NeonizeBackend:
    """Create a NeonizeBackend with a test config."""
    cfg = WhatsAppConfig(neonize=NeonizeConfig(db_path="/tmp/test_neonize.db"))
    return NeonizeBackend(cfg)


def _make_mock_client() -> MagicMock:
    """Create a mock neonize client with event registration."""
    client = MagicMock()
    client.is_connected = False
    client.connect = MagicMock()
    client.disconnect = MagicMock()
    client.send_message = MagicMock(return_value=MagicMock(key=MagicMock(ID="msg_123")))
    # The event decorator returns a callable that registers the handler
    client.event = MagicMock(side_effect=lambda ev_cls: lambda fn: fn)
    return client


# ── _is_connection_error ────────────────────────────────────────────────────


class TestIsConnectionError:
    def test_detects_known_markers(self):
        for marker in _CONNECTION_ERROR_MARKERS:
            assert _is_connection_error(Exception(marker))

    def test_case_insensitive(self):
        assert _is_connection_error(Exception("CONNECTION RESET"))

    def test_unknown_error_returns_false(self):
        assert not _is_connection_error(Exception("something else entirely"))

    def test_empty_message_returns_false(self):
        assert not _is_connection_error(Exception(""))


# ── _parse_jid ──────────────────────────────────────────────────────────────


class TestParseJid:
    def test_at_separator(self):
        user, server = _parse_jid("1234567890@s.whatsapp.net")
        assert user == "1234567890"
        assert server == "s.whatsapp.net"

    def test_at_separator_custom_server(self):
        user, server = _parse_jid("1234567890@g.us")
        assert user == "1234567890"
        assert server == "g.us"

    def test_at_without_dot_falls_back(self):
        user, server = _parse_jid("1234567890@nodots")
        assert user == "1234567890"
        assert server == "s.whatsapp.net"

    def test_underscore_separator(self):
        user, server = _parse_jid("1234567890_s.whatsapp.net")
        assert user == "1234567890"
        assert server == "s.whatsapp.net"

    def test_no_separator_falls_back_to_default(self):
        user, server = _parse_jid("1234567890")
        assert user == "1234567890"
        assert server == "s.whatsapp.net"


# ── _extract_message ────────────────────────────────────────────────────────


class TestExtractMessage:
    def _make_event(
        self,
        *,
        msg_id: str = "msg_1",
        chat_user: str = "1234",
        chat_server: str = "s.whatsapp.net",
        sender_user: str = "5678",
        sender_server: str = "s.whatsapp.net",
        is_group: bool = False,
        is_from_me: bool = False,
        text: str = "hello",
        pushname: str = "Test",
        timestamp: int = 1000,
    ) -> MagicMock:
        """Build a mock neonize MessageEv."""
        chat_jid = MagicMock()
        chat_jid.User = chat_user
        chat_jid.Server = chat_server
        sender_jid = MagicMock()
        sender_jid.User = sender_user
        sender_jid.Server = sender_server

        source = MagicMock()
        source.Chat = chat_jid
        source.Sender = sender_jid
        source.IsGroup = is_group
        source.IsFromMe = is_from_me

        info = MagicMock()
        info.ID = msg_id
        info.MessageSource = source
        info.Pushname = pushname
        info.Timestamp = timestamp

        msg = MagicMock()
        msg.conversation = text
        msg.extendedTextMessage = None

        ev = MagicMock()
        ev.Info = info
        ev.Message = msg
        return ev

    def test_valid_dm_from_other(self):
        ev = self._make_event()
        result = _extract_message(ev)
        assert result is not None
        assert result["id"] == "msg_1"
        assert result["chat_id"] == "1234@s.whatsapp.net"
        assert result["sender_id"] == "5678"
        assert result["text"] == "hello"
        assert result["fromMe"] is False
        assert result["toMe"] is True  # DM, not from me

    def test_valid_from_me_dm(self):
        ev = self._make_event(is_from_me=True, chat_user="1234", sender_user="1234")
        result = _extract_message(ev)
        assert result is not None
        assert result["fromMe"] is True
        # toMe = not is_group and (not from_me or sender_str == chat_str)
        # sender_str == chat_str → True, so toMe is True
        assert result["toMe"] is True

    def test_group_message(self):
        ev = self._make_event(is_group=True)
        result = _extract_message(ev)
        assert result is not None
        assert result["toMe"] is False

    def test_empty_text_returns_none(self):
        ev = self._make_event(text="")
        msg = ev.Message
        msg.conversation = None
        msg.extendedTextMessage = MagicMock()
        msg.extendedTextMessage.text = None
        result = _extract_message(ev)
        assert result is None

    def test_extended_text_message(self):
        ev = self._make_event(text="")
        msg = ev.Message
        msg.conversation = None
        ext = MagicMock()
        ext.text = "extended text"
        msg.extendedTextMessage = ext
        result = _extract_message(ev)
        assert result is not None
        assert result["text"] == "extended text"

    def test_empty_chat_str_returns_none(self):
        ev = self._make_event(chat_user="")
        result = _extract_message(ev)
        assert result is None

    def test_empty_sender_id_returns_none(self):
        ev = self._make_event(sender_user="", chat_user="")
        result = _extract_message(ev)
        assert result is None

    def test_exception_returns_none(self):
        ev = MagicMock()
        ev.Info = MagicMock(side_effect=AttributeError("broken"))
        del ev.Info.MessageSource  # force error
        result = _extract_message(ev)
        assert result is None


# ── NeonizeBackend.is_connected ─────────────────────────────────────────────


class TestIsConnected:
    def test_no_client_returns_false(self):
        backend = _make_backend()
        assert backend.is_connected is False

    def test_client_connected(self):
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = True
        assert backend.is_connected is True

    def test_client_check_raises_falls_back_to_flag(self):
        backend = _make_backend()
        client = _make_mock_client()
        type(client).is_connected = property(lambda self: 1 / 0)  # type: ignore[misc]
        backend._client = client
        backend._connected = True
        assert backend.is_connected is True

    def test_is_ready_reflects_ready_event(self):
        backend = _make_backend()
        assert backend.is_ready is False
        backend._ready_event.set()
        assert backend.is_ready is True


# ── _wait_for_connection ────────────────────────────────────────────────────


class TestWaitForConnection:
    @pytest.mark.asyncio
    async def test_returns_true_when_already_connected(self):
        backend = _make_backend()
        backend._connected = True
        backend._client = _make_mock_client()
        backend._client.is_connected = True
        assert await backend._wait_for_connection(timeout=5) is True

    @pytest.mark.asyncio
    async def test_returns_false_on_timeout(self):
        backend = _make_backend()
        backend._client = None  # never connects
        assert await backend._wait_for_connection(timeout=0.1) is False

    @pytest.mark.asyncio
    async def test_connects_during_wait(self):
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False

        async def _connect_after_delay():
            await asyncio.sleep(0.1)
            backend._client.is_connected = True
            backend._connected = True

        asyncio.get_event_loop().create_task(_connect_after_delay())
        assert await backend._wait_for_connection(timeout=2) is True


# ── Watchdog ────────────────────────────────────────────────────────────────


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_watchdog_exits_on_generation_change(self):
        """Stale watchdog (old generation) exits immediately."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        old_gen = backend._watchdog_gen  # 0
        backend._watchdog_gen = old_gen + 1  # simulate new start()

        # Should exit quickly — no sleep cycles
        start = time.time()
        await backend._watchdog(old_gen)
        elapsed = time.time() - start
        assert elapsed < _WATCHDOG_INTERVAL + 1

    @pytest.mark.asyncio
    async def test_watchdog_exits_on_client_none(self):
        """Watchdog exits cleanly when client is torn down."""
        backend = _make_backend()
        backend._client = None
        gen = backend._watchdog_gen
        await backend._watchdog(gen)
        # Should not hang

    @pytest.mark.asyncio
    async def test_watchdog_detects_network_outage(self):
        """Watchdog sets _network_outage flag when internet is unreachable."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False
        backend._connected = False
        backend._disconnect_time = time.time()
        gen = backend._watchdog_gen

        # Short-circuit: change generation after first iteration
        async def _bump_gen():
            await asyncio.sleep(_WATCHDOG_INTERVAL + 0.5)
            backend._watchdog_gen = gen + 1

        asyncio.get_event_loop().create_task(_bump_gen())

        with patch(
            "src.channels.neonize_backend._internet_available", return_value=False
        ):
            await backend._watchdog(gen)

        assert backend._network_outage is True

    @pytest.mark.asyncio
    async def test_watchdog_triggers_reconnect_on_internet_recovery(self):
        """Watchdog reconnects when internet returns after outage."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False
        backend._connected = False
        backend._disconnect_time = time.time()
        backend._network_outage = True
        gen = backend._watchdog_gen

        call_count = 0

        async def fake_reconnect():
            nonlocal call_count
            call_count += 1
            # After first reconnect, stop watchdog by bumping generation
            backend._watchdog_gen = gen + 1

        with (
            patch("src.channels.neonize_backend._internet_available", return_value=True),
            patch.object(backend, "_reconnect", side_effect=fake_reconnect),
            patch("src.channels.neonize_backend.asyncio.sleep", new_callable=AsyncMock, return_value=None),
        ):
            await backend._watchdog(gen)

        assert call_count == 1
        assert backend._network_outage is False

    @pytest.mark.asyncio
    async def test_watchdog_skips_reconnect_if_still_connected(self):
        """Watchdog does nothing when connected."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = True
        backend._connected = True
        gen = backend._watchdog_gen

        async def _bump_gen():
            await asyncio.sleep(_WATCHDOG_INTERVAL + 0.5)
            backend._watchdog_gen = gen + 1

        asyncio.get_event_loop().create_task(_bump_gen())

        with (
            patch("src.channels.neonize_backend._internet_available") as mock_net,
            patch.object(backend, "_reconnect", new_callable=AsyncMock) as mock_reconnect,
        ):
            await backend._watchdog(gen)

        mock_net.assert_not_called()
        mock_reconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_watchdog_backoff_increases_on_consecutive_failures(self):
        """Reconnection delay increases exponentially after each failure."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False
        backend._connected = False
        backend._disconnect_time = time.time()
        backend._network_outage = True
        gen = backend._watchdog_gen

        reconnect_calls = 0
        sleep_durations: list[float] = []

        async def failing_reconnect():
            nonlocal reconnect_calls
            reconnect_calls += 1
            if reconnect_calls >= 3:
                backend._watchdog_gen = gen + 1  # stop watchdog
            raise RuntimeError("reconnect failed")

        async def fake_sleep(delay):
            sleep_durations.append(delay)

        with (
            patch("src.channels.neonize_backend._internet_available", return_value=True),
            patch.object(backend, "_reconnect", side_effect=failing_reconnect),
            patch("src.channels.neonize_backend.asyncio.sleep", new_callable=AsyncMock, side_effect=fake_sleep),
        ):
            await backend._watchdog(gen)

        # First failure: delay ~5s, second failure: delay ~10s
        assert reconnect_calls == 3
        # The backoff sleep is after the _WATCHDOG_INTERVAL sleep
        # Filter only the backoff sleeps (those with jitter, so > _WATCHDOG_INTERVAL * 0.5)
        backoff_sleeps = [s for s in sleep_durations if s != _WATCHDOG_INTERVAL]
        assert len(backoff_sleeps) >= 2
        # Second backoff sleep should be larger than first
        assert backoff_sleeps[1] > backoff_sleeps[0] * 1.5

    @pytest.mark.asyncio
    async def test_watchdog_backoff_resets_on_success(self):
        """Reconnection delay resets to base interval after successful reconnect."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False
        backend._connected = False
        backend._disconnect_time = time.time()
        backend._network_outage = True
        gen = backend._watchdog_gen
        backend._reconnect_delay = 40.0  # simulate previous backoff

        async def succeed_reconnect():
            backend._watchdog_gen = gen + 1  # stop after one iteration

        with (
            patch("src.channels.neonize_backend._internet_available", return_value=True),
            patch.object(backend, "_reconnect", side_effect=succeed_reconnect),
            patch("src.channels.neonize_backend.asyncio.sleep", new_callable=AsyncMock, return_value=None),
        ):
            await backend._watchdog(gen)

        assert backend._reconnect_delay == _WATCHDOG_INTERVAL

    @pytest.mark.asyncio
    async def test_watchdog_backoff_resets_when_already_connected(self):
        """Backoff resets when the watchdog finds the connection already restored."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = True
        backend._connected = True
        gen = backend._watchdog_gen
        backend._reconnect_delay = 40.0  # simulate previous backoff

        # Stop the watchdog after 2 sleep calls by bumping generation
        sleep_count = 0

        async def counting_sleep(delay):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                backend._watchdog_gen = gen + 1

        with patch(
            "src.channels.neonize_backend.asyncio.sleep",
            new_callable=AsyncMock,
            side_effect=counting_sleep,
        ):
            await backend._watchdog(gen)

        assert backend._reconnect_delay == _WATCHDOG_INTERVAL

    @pytest.mark.asyncio
    async def test_watchdog_backoff_resets_after_full_failure_success_cycle(self):
        """After failures increase delay and a success resets it, next failure starts from base delay.

        Simulates: fail → fail (delay grows) → succeed (delay resets) → fail
        Asserts the post-success failure uses the base delay, not the elevated one.
        """
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False
        backend._connected = False
        backend._disconnect_time = time.time()
        backend._network_outage = True
        gen = backend._watchdog_gen

        call_count = 0

        async def reconnect_with_cycle():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                # First 2 calls: fail — delay increases each time
                raise RuntimeError("reconnect failed")
            elif call_count == 3:
                # 3rd call: succeed — watchdog resets delay to _WATCHDOG_INTERVAL
                # Keep disconnected so the next iteration enters the reconnect path
                backend._connected = False
                backend._client.is_connected = False
            else:
                # 4th call: fail again — should start from _WATCHDOG_INTERVAL
                backend._watchdog_gen = gen + 1  # stop watchdog
                raise RuntimeError("reconnect failed again")

        with (
            patch("src.channels.neonize_backend._internet_available", return_value=True),
            patch.object(backend, "_reconnect", side_effect=reconnect_with_cycle),
            patch("src.channels.neonize_backend.asyncio.sleep", new_callable=AsyncMock, return_value=None),
        ):
            await backend._watchdog(gen)

        # Delay should be base * BACKOFF_MULTIPLIER (one step from reset),
        # not the elevated value from earlier failures.
        assert backend._reconnect_delay == _WATCHDOG_INTERVAL * BACKOFF_MULTIPLIER

    @pytest.mark.asyncio
    async def test_watchdog_backoff_capped_at_max(self):
        """Reconnection delay never exceeds _MAX_RECONNECT_DELAY."""
        backend = _make_backend()
        backend._client = _make_mock_client()
        backend._client.is_connected = False
        backend._connected = False
        backend._disconnect_time = time.time()
        backend._network_outage = True
        gen = backend._watchdog_gen
        backend._reconnect_delay = _MAX_RECONNECT_DELAY  # already at max

        call_count = 0

        async def failing_reconnect():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                backend._watchdog_gen = gen + 1
            raise RuntimeError("reconnect failed")

        with (
            patch("src.channels.neonize_backend._internet_available", return_value=True),
            patch.object(backend, "_reconnect", side_effect=failing_reconnect),
            patch("src.channels.neonize_backend.asyncio.sleep", new_callable=AsyncMock, return_value=None),
        ):
            await backend._watchdog(gen)

        # _reconnect_delay should still be capped at max
        assert backend._reconnect_delay <= _MAX_RECONNECT_DELAY


# ── Message queue bridge ────────────────────────────────────────────────────


class TestMessageQueue:
    @pytest.mark.asyncio
    async def test_poll_message_returns_none_on_empty(self):
        backend = _make_backend()
        result = await backend.poll_message()
        assert result is None

    @pytest.mark.asyncio
    async def test_poll_message_returns_queued_item(self):
        backend = _make_backend()
        msg = {"id": "m1", "chat_id": "c1", "text": "hi"}
        await backend._message_queue.put(msg)
        result = await backend.poll_message()
        assert result == msg

    @pytest.mark.asyncio
    async def test_message_bridge_from_thread(self):
        """Verify that putting a message from a thread is visible to poll_message."""
        backend = _make_backend()
        loop = asyncio.get_event_loop()
        backend._loop = loop
        backend._ready_event.set()
        backend._connected_at = time.time()

        msg = {
            "id": "m1",
            "chat_id": "1234@s.whatsapp.net",
            "sender_id": "1234",
            "sender_name": "Tester",
            "text": "from thread",
            "timestamp": time.time(),
            "fromMe": False,
            "toMe": True,
        }

        # Simulate neonize thread putting message via run_coroutine_threadsafe
        def _put_from_thread():
            asyncio.run_coroutine_threadsafe(backend._message_queue.put(msg), loop)

        t = threading.Thread(target=_put_from_thread, daemon=True)
        t.start()
        t.join(timeout=2)

        result = await backend.poll_message()
        assert result is not None
        assert result["text"] == "from thread"


# ── Reconnect ───────────────────────────────────────────────────────────────


class TestReconnect:
    @pytest.mark.asyncio
    async def test_reconnect_raises_on_timeout(self):
        """Reconnect raises RuntimeError if ready_event never fires."""
        backend = _make_backend()
        backend._loop = asyncio.get_event_loop()

        with (
            patch(
                "src.channels.neonize_backend.NeonizeBackend.start", MagicMock()
            ),
            pytest.raises(RuntimeError, match="timed out"),
        ):
            # timeout=0 so ready_event.wait returns immediately (not set)
            await backend._reconnect(timeout=0.01)

    @pytest.mark.asyncio
    async def test_reconnect_clears_state_and_calls_start(self):
        """Reconnect clears state and calls start() with the stored loop."""
        backend = _make_backend()
        backend._loop = asyncio.get_event_loop()
        backend._connected = True
        backend._client = _make_mock_client()

        async def _set_ready():
            await asyncio.sleep(0.05)
            backend._ready_event.set()

        asyncio.get_event_loop().create_task(_set_ready())

        with patch(
            "src.channels.neonize_backend.NeonizeBackend.start", MagicMock()
        ) as mock_start:
            await backend._reconnect(timeout=2)

        mock_start.assert_called_once_with(backend._loop)
        assert backend._connected is False


# ── Send ────────────────────────────────────────────────────────────────────


class TestSend:
    @pytest.mark.asyncio
    async def test_send_waits_for_reconnection(self):
        """send() waits for connection when disconnected, then sends."""
        backend = _make_backend()
        backend._loop = asyncio.get_event_loop()
        client = _make_mock_client()
        client.is_connected = False
        backend._client = client

        async def _connect_after_delay():
            await asyncio.sleep(0.1)
            client.is_connected = True
            backend._connected = True

        asyncio.get_event_loop().create_task(_connect_after_delay())

        with (
            patch("src.channels.neonize_backend._parse_jid", return_value=("u", "s.whatsapp.net")),
            patch("src.channels.neonize_backend._build_jid", return_value=MagicMock()),
        ):
            await backend.send("chat1", "hello", skip_delays=True)

    @pytest.mark.asyncio
    async def test_send_raises_when_not_connected(self):
        """send() raises RuntimeError when reconnection fails."""
        backend = _make_backend()
        backend._client = None

        with patch.object(
            backend, "_wait_for_connection", new_callable=AsyncMock, return_value=False
        ):
            with pytest.raises(RuntimeError, match="Not connected"):
                await backend.send("chat1", "hello")

    @pytest.mark.asyncio
    async def test_send_clears_typing_after_retry_failure(self):
        """Typing indicator is cleared when reconnection retry also fails."""
        backend = _make_backend()
        backend._loop = asyncio.get_event_loop()
        client = _make_mock_client()
        client.is_connected = True
        backend._client = client

        # First send raises a connection error, triggering reconnect+retry path
        client.send_message = MagicMock(
            side_effect=RuntimeError("usync: connection stale")
        )

        with (
            patch("src.channels.neonize_backend._parse_jid", return_value=("u", "s.whatsapp.net")),
            patch("src.channels.neonize_backend._build_jid", return_value=MagicMock()),
            patch.object(backend, "_reconnect", new_callable=AsyncMock),
            patch.object(backend, "set_typing", new_callable=AsyncMock) as mock_typing,
            pytest.raises(RuntimeError, match="usync"),
        ):
            await backend.send("chat1", "hello", skip_delays=True)

        # Verify set_typing was called with composing=False (at least once
        # from the retry error path, and once from the outer finally)
        off_calls = [c for c in mock_typing.call_args_list if not c.kwargs.get("composing", True)]
        assert len(off_calls) >= 1, (
            f"Expected set_typing(composing=False) but got calls: {mock_typing.call_args_list}"
        )


# ── Disconnect ──────────────────────────────────────────────────────────────


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_clears_state(self):
        backend = _make_backend()
        client = _make_mock_client()
        backend._client = client
        backend._connected = True

        await backend.disconnect()

        assert backend._client is None
        assert backend._connected is False
        client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_tolerates_client_error(self):
        backend = _make_backend()
        client = _make_mock_client()
        client.disconnect.side_effect = RuntimeError("boom")
        backend._client = client

        # Should not raise
        await backend.disconnect()
        assert backend._client is None

    @pytest.mark.asyncio
    async def test_disconnect_with_no_client(self):
        backend = _make_backend()
        backend._client = None
        await backend.disconnect()  # should not raise


# Import at module level for the interval constant
from src.channels.neonize_backend import _WATCHDOG_INTERVAL
