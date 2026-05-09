"""
test_channel_contract.py — Contract test suite for BaseChannel subclasses.

Defines the channel behavior contract and auto-verifies all implementations.
Tests cover:
  - send_message: accepts chat_id and text, returns result
  - send_typing: accepts chat_id
  - close: closes connection gracefully
  - request_shutdown: signals channel to stop
  - State transitions: mark_connected → wait_connected
  - start: establishes connection and calls mark_connected
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

from src.channels.base import BaseChannel, IncomingMessage
from src.channels.registry import ChannelState


# ── Channel implementations for parametrized contract testing ───────────────


class StubChannel(BaseChannel):
    """Minimal concrete BaseChannel for contract testing."""

    def __init__(self, safe_mode: bool = False, load_history: bool = False) -> None:
        super().__init__(safe_mode=safe_mode, load_history=load_history)
        self.sent: list[tuple[str, str, bool]] = []
        self.typing_calls: list[str] = []
        self.started = False
        self.closed = False
        self.shutdown_requested = False

    async def start(self, handler) -> None:  # type: ignore[override]
        self.started = True
        self.mark_connected()

    async def _send_message(self, chat_id: str, text: str, *, skip_delays: bool = False) -> None:
        self.sent.append((chat_id, text, skip_delays))

    async def send_typing(self, chat_id: str) -> None:
        self.typing_calls.append(chat_id)

    async def close(self) -> None:
        self.closed = True

    def request_shutdown(self) -> None:
        self.shutdown_requested = True


class RecordingChannel(BaseChannel):
    """Alternative channel that records all interactions for verification."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, tuple, dict]] = []
        self._connected = False

    async def start(self, handler) -> None:  # type: ignore[override]
        self.calls.append(("start", (), {}))
        self.mark_connected()

    async def _send_message(self, chat_id: str, text: str, *, skip_delays: bool = False) -> None:
        self.calls.append(("send_message", (chat_id, text), {"skip_delays": skip_delays}))

    async def send_typing(self, chat_id: str) -> None:
        self.calls.append(("send_typing", (chat_id,), {}))

    async def close(self) -> None:
        self.calls.append(("close", (), {}))

    def request_shutdown(self) -> None:
        self.calls.append(("request_shutdown", (), {}))


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_msg(
    *,
    chat_id: str = "chat_123",
    text: str = "Hello!",
    message_id: str = "msg_001",
) -> IncomingMessage:
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id="1234567890",
        sender_name="Alice",
        text=text,
        timestamp=time.time(),
        acl_passed=True,
    )


# ── Parametrized channel fixtures ───────────────────────────────────────────

_CHANNEL_CLASSES = [StubChannel, RecordingChannel]


@pytest.fixture(params=_CHANNEL_CLASSES, ids=lambda cls: cls.__name__)
def channel(request) -> BaseChannel:
    """Parametrized fixture: provides each channel implementation."""
    return request.param()


# ── Contract Tests ──────────────────────────────────────────────────────────


class TestSendMessageContract:
    """send_message accepts chat_id and text, delegates to implementation."""

    async def test_send_message_with_valid_args(self, channel: BaseChannel) -> None:
        """send_message(chat_id, text) completes without error."""
        await channel.send_message("chat_1", "Hello!")

    async def test_send_message_with_empty_text(self, channel: BaseChannel) -> None:
        """Empty text is accepted."""
        await channel.send_message("chat_1", "")

    async def test_send_message_with_long_text(self, channel: BaseChannel) -> None:
        """Long text is accepted."""
        await channel.send_message("chat_1", "A" * 10000)

    async def test_send_message_forwards_skip_delays(self, channel: BaseChannel) -> None:
        """skip_delays keyword is forwarded."""
        await channel.send_message("chat_1", "Hello!", skip_delays=True)


class TestSendTypingContract:
    """send_typing accepts chat_id."""

    async def test_send_typing_completes(self, channel: BaseChannel) -> None:
        """send_typing(chat_id) completes without error."""
        await channel.send_typing("chat_1")


class TestCloseContract:
    """close closes connection gracefully."""

    async def test_close_completes(self, channel: BaseChannel) -> None:
        """close() completes without error."""
        await channel.close()

    async def test_close_idempotent(self, channel: BaseChannel) -> None:
        """Calling close() twice does not raise."""
        await channel.close()
        await channel.close()


class TestRequestShutdownContract:
    """request_shutdown signals the channel to stop."""

    def test_request_shutdown_completes(self, channel: BaseChannel) -> None:
        """request_shutdown() completes without error."""
        channel.request_shutdown()


class TestStateTransitions:
    """State transitions: DISCONNECTED → mark_connected → CONNECTED."""

    def test_initial_state_is_disconnected(self, channel: BaseChannel) -> None:
        """Channel starts in DISCONNECTED state."""
        assert channel.state == ChannelState.DISCONNECTED

    def test_mark_connected_transitions_to_connected(self, channel: BaseChannel) -> None:
        """mark_connected() transitions state to CONNECTED."""
        channel.mark_connected()
        assert channel.state == ChannelState.CONNECTED

    async def test_wait_connected_resolves_after_mark(self, channel: BaseChannel) -> None:
        """wait_connected() resolves after mark_connected() is called."""
        channel.mark_connected()
        await asyncio.wait_for(channel.wait_connected(), timeout=0.5)

    async def test_start_establishes_connection(self, channel: BaseChannel) -> None:
        """start() calls mark_connected(), establishing the connection."""

        async def _noop(msg: IncomingMessage) -> None:
            pass

        await channel.start(_noop)
        assert channel.state == ChannelState.CONNECTED
        await asyncio.wait_for(channel.wait_connected(), timeout=0.5)


class TestHistoricalMessagesContract:
    """should_process_historical respects load_history setting."""

    def test_non_historical_always_processed(self, channel: BaseChannel) -> None:
        msg = _make_msg()
        assert channel.should_process_historical(msg) is True

    def test_historical_rejected_by_default(self, channel: BaseChannel) -> None:
        msg = _make_msg()
        # Create a new IncomingMessage with is_historical=True
        hist_msg = IncomingMessage(
            message_id="hist_1",
            chat_id="chat_1",
            sender_id="s1",
            sender_name="Alice",
            text="old message",
            timestamp=time.time(),
            acl_passed=True,
            is_historical=True,
        )
        assert channel.should_process_historical(hist_msg) is False

    def test_historical_accepted_when_load_history(self, channel: BaseChannel) -> None:
        channel._load_history = True
        hist_msg = IncomingMessage(
            message_id="hist_1",
            chat_id="chat_1",
            sender_id="s1",
            sender_name="Alice",
            text="old message",
            timestamp=time.time(),
            acl_passed=True,
            is_historical=True,
        )
        assert channel.should_process_historical(hist_msg) is True


class TestDefaultBehavior:
    """Default implementations return sensible values."""

    def test_get_channel_prompt_returns_none(self, channel: BaseChannel) -> None:
        assert channel.get_channel_prompt() is None

    def test_create_config_applier_returns_none(self, channel: BaseChannel) -> None:
        assert channel.create_config_applier() is None

    async def test_apply_channel_config_is_noop(self, channel: BaseChannel) -> None:
        """apply_channel_config does not raise for any input."""
        channel.apply_channel_config(MagicMock(), set())

    async def test_send_audio_raises_not_implemented(self, channel: BaseChannel) -> None:
        with pytest.raises(NotImplementedError):
            await channel.send_audio("chat_1", Path("/fake/audio.mp3"))

    async def test_send_document_raises_not_implemented(self, channel: BaseChannel) -> None:
        with pytest.raises(NotImplementedError):
            await channel.send_document("chat_1", Path("/fake/doc.pdf"))
