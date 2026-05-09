"""
test_base_channel_contract.py — Contract test suite for the BaseChannel ABC.

Provides:

- ``StubChannel``: minimal concrete BaseChannel for isolated contract testing.
- ``TestBaseChannelABC``: verifies ABC enforcement (cannot instantiate directly,
  incomplete subclasses raise TypeError, complete subclass works).
- ``BaseChannelTestMixin``: mixin with tests every BaseChannel implementation
  should pass.  Inherit in channel-specific test classes to verify compliance.
- ``TestBaseChannelContract``: direct tests using StubChannel + mixin covering
  send_and_track, safe mode, historical messages, and connection events.

Usage for channel implementations::

    from tests.unit.test_base_channel_contract import BaseChannelTestMixin

    class TestMyChannelContract(BaseChannelTestMixin):
        @pytest.fixture(autouse=True)
        def _init_channel(self):
            self.channel = MyChannel(...)

        # Override or skip tests for features the channel implements
        # differently (e.g. send_audio, get_channel_prompt).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.channels.base import BaseChannel, IncomingMessage


# ─────────────────────────────────────────────────────────────────────────────
# Helper: StubChannel
# ─────────────────────────────────────────────────────────────────────────────


class StubChannel(BaseChannel):
    """Minimal concrete BaseChannel for contract testing.

    Records all calls so tests can verify delegation without side effects.
    """

    def __init__(self, safe_mode: bool = False, load_history: bool = False) -> None:
        super().__init__(safe_mode=safe_mode, load_history=load_history)
        self.sent: list[tuple[str, str, bool]] = []  # (chat_id, text, skip_delays)
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


# ─────────────────────────────────────────────────────────────────────────────
# Helper: message factory
# ─────────────────────────────────────────────────────────────────────────────


def _make_msg(
    *,
    chat_id: str = "chat_123",
    text: str = "Hello!",
    message_id: str = "msg_001",
    is_historical: bool = False,
) -> IncomingMessage:
    """Create a valid IncomingMessage with sensible test defaults."""
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id="1234567890",
        sender_name="Alice",
        text=text,
        timestamp=time.time(),
        acl_passed=True,
        is_historical=is_historical,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Test: ABC contract enforcement
# ─────────────────────────────────────────────────────────────────────────────


class TestBaseChannelABC:
    """Verify the BaseChannel ABC cannot be instantiated directly and that
    subclasses must implement every abstract method."""

    def test_cannot_instantiate_abc_directly(self):
        """BaseChannel() raises TypeError because it is abstract."""
        with pytest.raises(TypeError, match="abstract method"):
            BaseChannel()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_abstract_methods(self):
        """A subclass missing any abstract method raises TypeError on instantiation."""

        class _IncompleteChannel(BaseChannel):
            """Only implements some abstract methods — not enough."""

            async def start(self, handler): ...  # type: ignore[override]
            # Missing: _send_message, send_typing, close, request_shutdown

        with pytest.raises(TypeError, match="abstract method"):
            _IncompleteChannel()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self):
        """A subclass that implements all abstract methods can be instantiated."""
        channel = StubChannel()
        assert isinstance(channel, BaseChannel)


# ─────────────────────────────────────────────────────────────────────────────
# BaseChannelTestMixin
# ─────────────────────────────────────────────────────────────────────────────


class BaseChannelTestMixin:
    """Shared contract tests for BaseChannel implementations.

    Subclasses must set ``self.channel`` via an ``autouse`` fixture or
    ``setUp`` hook.  The channel should be a fresh instance with
    ``safe_mode=False`` and ``load_history=False`` (the defaults).

    Channel implementations that override concrete base methods (e.g.
    ``send_audio``, ``get_channel_prompt``) should override or skip the
    corresponding mixin tests.
    """

    channel: BaseChannel

    # ── mark_connected / wait_connected ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mark_connected_resolves_wait_connected(self):
        """mark_connected() unblocks wait_connected()."""
        self.channel.mark_connected()
        await asyncio.wait_for(self.channel.wait_connected(), timeout=0.5)

    @pytest.mark.asyncio
    async def test_wait_connected_blocks_until_mark_connected(self):
        """wait_connected() blocks until mark_connected() is called."""
        async def _mark_soon():
            await asyncio.sleep(0.05)
            self.channel.mark_connected()

        asyncio.ensure_future(_mark_soon())
        await asyncio.wait_for(self.channel.wait_connected(), timeout=0.5)

    @pytest.mark.asyncio
    async def test_mark_connected_idempotent(self):
        """Calling mark_connected() multiple times does not raise."""
        self.channel.mark_connected()
        self.channel.mark_connected()
        await asyncio.wait_for(self.channel.wait_connected(), timeout=0.5)

    # ── send_message (normal mode) ─────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_send_message_delegates_to_impl(self):
        """send_message() calls _send_message with the provided arguments."""
        with patch.object(self.channel, "_send_message", new_callable=AsyncMock) as mock:
            await self.channel.send_message("chat_1", "Hello!")
        mock.assert_called_once_with("chat_1", "Hello!", skip_delays=False)

    @pytest.mark.asyncio
    async def test_send_message_forwards_skip_delays(self):
        """skip_delays keyword is forwarded to _send_message."""
        with patch.object(self.channel, "_send_message", new_callable=AsyncMock) as mock:
            await self.channel.send_message("chat_1", "Hello!", skip_delays=True)
        mock.assert_called_once_with("chat_1", "Hello!", skip_delays=True)

    # ── send_message (safe mode) ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_safe_mode_confirmed_delegates_to_impl(self):
        """In safe mode, a confirmed send calls _send_message."""
        self.channel._safe_mode = True
        with (
            patch("src.channels.base._confirm_send", new_callable=AsyncMock, return_value=True),
            patch.object(self.channel, "_send_message", new_callable=AsyncMock) as mock_send,
        ):
            await self.channel.send_message("chat_1", "Hello!")
        mock_send.assert_called_once_with("chat_1", "Hello!", skip_delays=False)

    @pytest.mark.asyncio
    async def test_safe_mode_rejected_does_not_call_impl(self):
        """In safe mode, a rejected send does NOT call _send_message."""
        self.channel._safe_mode = True
        with (
            patch("src.channels.base._confirm_send", new_callable=AsyncMock, return_value=False),
            patch.object(self.channel, "_send_message", new_callable=AsyncMock) as mock_send,
        ):
            await self.channel.send_message("chat_1", "Hello!")
        mock_send.assert_not_called()

    # ── send_and_track ─────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_send_and_track_calls_send_message(self):
        """send_and_track() delegates to send_message()."""
        with (
            patch.object(self.channel, "send_message", new_callable=AsyncMock) as mock_send,
            patch("src.core.event_bus.get_event_bus"),
        ):
            await self.channel.send_and_track("chat_1", "Hi!")
        mock_send.assert_called_once_with("chat_1", "Hi!", skip_delays=False)

    @pytest.mark.asyncio
    async def test_send_and_track_records_outbound_dedup(self):
        """send_and_track() records outbound dedup when a dedup service is provided."""
        dedup = MagicMock()
        with (
            patch.object(self.channel, "send_message", new_callable=AsyncMock),
            patch("src.core.event_bus.get_event_bus") as mock_get_bus,
        ):
            mock_get_bus.return_value.emit = AsyncMock()
            await self.channel.send_and_track("chat_1", "Hi!", dedup=dedup)
        dedup.record_outbound_keyed.assert_called_once()
        # Verify the key was computed from (chat_id, text) via outbound_key()
        recorded_key = dedup.record_outbound_keyed.call_args[0][0]
        from src.core.dedup import outbound_key
        assert recorded_key == outbound_key("chat_1", "Hi!")

    @pytest.mark.asyncio
    async def test_send_and_track_without_dedup(self):
        """send_and_track() works without a dedup service (dedup=None)."""
        with (
            patch.object(self.channel, "send_message", new_callable=AsyncMock),
            patch("src.core.event_bus.get_event_bus") as mock_get_bus,
        ):
            mock_get_bus.return_value.emit = AsyncMock()
            # Should not raise
            await self.channel.send_and_track("chat_1", "Hi!", dedup=None)

    @pytest.mark.asyncio
    async def test_send_and_track_emits_response_sent_event(self):
        """send_and_track() emits a 'response_sent' event via the event bus."""
        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()
        with (
            patch.object(self.channel, "send_message", new_callable=AsyncMock),
            patch("src.core.event_bus.get_event_bus", return_value=mock_bus),
        ):
            await self.channel.send_and_track("chat_1", "Hi!")

        mock_bus.emit.assert_called_once()
        event = mock_bus.emit.call_args[0][0]
        assert event.name == "response_sent"
        assert event.data["chat_id"] == "chat_1"
        assert event.data["response_length"] == len("Hi!")

    @pytest.mark.asyncio
    async def test_send_and_track_skips_dedup_and_event_on_send_failure(self):
        """send_and_track() does NOT record dedup or emit event when send fails."""
        dedup = MagicMock()
        mock_bus = MagicMock()
        mock_bus.emit = AsyncMock()
        with (
            patch.object(
                self.channel, "send_message", new_callable=AsyncMock, side_effect=RuntimeError("boom")
            ),
            patch("src.core.event_bus.get_event_bus", return_value=mock_bus),
        ):
            # Should not raise — send_and_track catches the send failure
            await self.channel.send_and_track("chat_1", "Hi!", dedup=dedup)

        # Dedup and event should NOT fire when the underlying send failed
        dedup.record_outbound_keyed.assert_not_called()
        mock_bus.emit.assert_not_called()

    # ── media methods ──────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_send_audio_default_raises_not_implemented(self):
        """Base send_audio() raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="does not support audio"):
            await self.channel.send_audio("chat_1", Path("/fake/audio.mp3"))

    @pytest.mark.asyncio
    async def test_send_document_default_raises_not_implemented(self):
        """Base send_document() raises NotImplementedError."""
        with pytest.raises(NotImplementedError, match="does not support document"):
            await self.channel.send_document("chat_1", Path("/fake/doc.pdf"))

    # ── should_process_historical ──────────────────────────────────────────

    def test_should_process_non_historical_always_true(self):
        """Non-historical messages are always processed."""
        msg = _make_msg(is_historical=False)
        assert self.channel.should_process_historical(msg) is True

    def test_should_process_historical_rejected_by_default(self):
        """Historical messages are rejected when load_history is False (default)."""
        msg = _make_msg(is_historical=True)
        assert self.channel.should_process_historical(msg) is False

    def test_should_process_historical_accepted_when_loading(self):
        """Historical messages are accepted when load_history=True."""
        self.channel._load_history = True
        msg = _make_msg(is_historical=True)
        assert self.channel.should_process_historical(msg) is True

    # ── config methods ─────────────────────────────────────────────────────

    def test_get_channel_prompt_default_none(self):
        """Base get_channel_prompt() returns None."""
        assert self.channel.get_channel_prompt() is None

    def test_create_config_applier_default_none(self):
        """Base create_config_applier() returns None."""
        assert self.channel.create_config_applier() is None

    def test_apply_channel_config_default_noop(self):
        """Base apply_channel_config() does not raise."""
        self.channel.apply_channel_config(MagicMock(), {"llm.model"})

    # ── abstract method satisfaction ────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_send_typing_does_not_raise(self):
        """send_typing() can be called without error."""
        await self.channel.send_typing("chat_1")

    @pytest.mark.asyncio
    async def test_close_does_not_raise(self):
        """close() completes without error."""
        await self.channel.close()

    def test_request_shutdown_does_not_raise(self):
        """request_shutdown() completes without error."""
        self.channel.request_shutdown()


# ─────────────────────────────────────────────────────────────────────────────
# Test: BaseChannel contract via StubChannel
# ─────────────────────────────────────────────────────────────────────────────


class TestBaseChannelContract(BaseChannelTestMixin):
    """Verify the full BaseChannel contract using the StubChannel.

    The mixin provides generic assertions (using mocks/spies) that work
    for any BaseChannel subclass.  This class adds StubChannel-specific
    integration tests that exercise the full call path without mocking.
    """

    @pytest.fixture(autouse=True)
    def _init_channel(self):
        self.channel = StubChannel()

    # ── StubChannel integration tests (no mocks) ───────────────────────────

    @pytest.mark.asyncio
    async def test_send_message_records_in_sent_list(self):
        """StubChannel._send_message records chat_id, text, and skip_delays."""
        await self.channel.send_message("chat_1", "Hello!")
        assert self.channel.sent == [("chat_1", "Hello!", False)]

    @pytest.mark.asyncio
    async def test_send_message_with_skip_delays_recorded(self):
        """StubChannel records skip_delays=True correctly."""
        await self.channel.send_message("chat_2", "Ping", skip_delays=True)
        assert self.channel.sent == [("chat_2", "Ping", True)]

    @pytest.mark.asyncio
    async def test_multiple_sends_accumulate(self):
        """Multiple send_message calls accumulate in the sent list."""
        await self.channel.send_message("a", "first")
        await self.channel.send_message("b", "second")
        assert len(self.channel.sent) == 2
        assert self.channel.sent[0] == ("a", "first", False)
        assert self.channel.sent[1] == ("b", "second", False)

    @pytest.mark.asyncio
    async def test_send_typing_records_call(self):
        """StubChannel records typing indicator calls."""
        await self.channel.send_typing("chat_1")
        assert self.channel.typing_calls == ["chat_1"]

    @pytest.mark.asyncio
    async def test_close_sets_flag(self):
        """StubChannel.close() sets the closed flag."""
        await self.channel.close()
        assert self.channel.closed is True

    def test_request_shutdown_sets_flag(self):
        """StubChannel.request_shutdown() sets the shutdown flag."""
        self.channel.request_shutdown()
        assert self.channel.shutdown_requested is True

    @pytest.mark.asyncio
    async def test_start_marks_connected(self):
        """StubChannel.start() calls mark_connected()."""
        async def _noop_handler(msg: IncomingMessage) -> None:
            pass

        await self.channel.start(_noop_handler)
        assert self.channel.started is True
        # wait_connected should resolve immediately
        await asyncio.wait_for(self.channel.wait_connected(), timeout=0.5)

    # ── safe-mode lock isolation ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_safe_mode_lock_is_per_instance(self):
        """Two BaseChannel instances have independent safe-mode locks.

        The safe-mode lock was previously a module-level AsyncLock, causing
        all channel instances to contend on the same lock (a problem in
        tests and multi-channel setups).  Moving it to ``self._safe_mode_lock``
        ensures each instance is isolated.
        """
        ch_a = StubChannel(safe_mode=True)
        ch_b = StubChannel(safe_mode=True)

        # Each instance must have its own lock object
        assert ch_a._safe_mode_lock is not ch_b._safe_mode_lock

        # Verify both locks work independently — holding lock A must not
        # block lock B from acquiring.
        async with ch_a._safe_mode_lock:
            # lock A is held; lock B should be acquirable without waiting
            async with asyncio.timeout(0.1):
                async with ch_b._safe_mode_lock:
                    pass  # acquired immediately — no contention
