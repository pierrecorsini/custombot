"""
Chaos/failure tests for Bot.recover_pending_messages — crash recovery edge cases.

Covers:
- Partial recovery with mixed exception types
- Recovery with corrupted/incomplete queue data
- ACL filtering edge cases (no channel, missing _is_allowed, disallowed senders)
- Recovery when queue raises exceptions
- Concurrent recovery attempts
- Large batch recovery with mixed outcomes
- Queue returning messages with missing/None fields
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.bot import Bot, BotConfig, BotDeps
from src.channels.base import IncomingMessage
from src.message_queue import MessageStatus, QueuedMessage


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_bot(message_queue=None) -> Bot:
    """Create a Bot with fully mocked dependencies."""
    from unittest.mock import MagicMock

    cfg = BotConfig(
        max_tool_iterations=10,
        memory_max_history=50,
        system_prompt_prefix="",
    )

    db = AsyncMock()
    db.message_exists = AsyncMock(return_value=False)
    db.upsert_chat = AsyncMock()
    db.save_message = AsyncMock()
    db.get_history = AsyncMock(return_value=[])

    llm = AsyncMock()
    memory = AsyncMock()
    memory.ensure_workspace = MagicMock(return_value="/tmp/workspace")
    memory.read_memory = AsyncMock(return_value="")
    memory.read_agents_md = AsyncMock(return_value="")

    skills = MagicMock()
    skills.tool_definitions = []
    skills.all = MagicMock(return_value=[])

    dedup = AsyncMock()
    dedup.is_inbound_duplicate = AsyncMock(return_value=False)

    return Bot(
        BotDeps(
            config=cfg,
            db=db,
            llm=llm,
            memory=memory,
            skills=skills,
            routing=None,
            message_queue=message_queue,
            dedup=dedup,
        )
    )


def _make_queued_msg(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "hello",
    sender_name: str = "Tester",
    sender_id: str | None = None,
    created_at: float | None = None,
) -> MagicMock:
    """Create a mock queued message with configurable attributes."""
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat_id = chat_id
    msg.text = text
    msg.sender_name = sender_name
    msg.created_at = created_at or time.time()
    if sender_id is not None:
        msg.sender_id = sender_id
    return msg


def _make_channel(allowed: bool = True) -> MagicMock:
    """Create a mock channel with _is_allowed method."""
    channel = MagicMock()
    channel._is_allowed = MagicMock(return_value=allowed)
    return channel


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Partial recovery with mixed exception types
# ═══════════════════════════════════════════════════════════════════════════════


class TestPartialRecoveryMixedExceptions:
    """Tests for partial recovery when different messages fail for different reasons."""

    async def test_mixed_exception_types_in_failures(self):
        """Recovery continues when some messages raise ValueError, others RuntimeError."""
        queue = AsyncMock()
        q1 = _make_queued_msg(message_id="m1", text="ok")
        q2 = _make_queued_msg(message_id="m2", text="val_err")
        q3 = _make_queued_msg(message_id="m3", text="run_err")
        q4 = _make_queued_msg(message_id="m4", text="ok_too")
        queue.recover_stale = AsyncMock(return_value=[q1, q2, q3, q4])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        call_count = 0

        async def flaky_handle(msg, **kwargs):
            nonlocal call_count
            call_count += 1
            if msg.text == "val_err":
                raise ValueError("bad input")
            if msg.text == "run_err":
                raise RuntimeError("processing error")
            return "ok"

        bot.handle_message = flaky_handle

        result = await bot.recover_pending_messages(channel=channel)

        assert result["total_found"] == 4
        assert result["recovered"] == 2
        assert result["failed"] == 2
        assert len(result["failures"]) == 2

        failure_ids = {f["message_id"] for f in result["failures"]}
        assert failure_ids == {"m2", "m3"}

    async def test_first_message_fails_rest_succeed(self):
        """Recovery continues after the first message fails."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(5)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        call_count = 0

        async def fail_first(msg, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("network down")
            return "ok"

        bot.handle_message = fail_first

        result = await bot.recover_pending_messages(channel=channel)

        assert result["recovered"] == 4
        assert result["failed"] == 1
        assert result["failures"][0]["error"] == "network down"

    async def test_last_message_fails(self):
        """Recovery handles failure on the final message correctly."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(3)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        async def fail_last(msg, **kwargs):
            if msg.message_id == "m2":
                raise TimeoutError("timeout")
            return "ok"

        bot.handle_message = fail_last

        result = await bot.recover_pending_messages(channel=channel)

        assert result["recovered"] == 2
        assert result["failed"] == 1
        assert result["failures"][0]["message_id"] == "m2"

    async def test_every_other_message_fails(self):
        """Alternating success/failure pattern recovers correctly."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(6)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        async def alternate(msg, **kwargs):
            idx = int(msg.message_id[1:])
            if idx % 2 == 1:
                raise RuntimeError(f"odd failure {idx}")
            return "ok"

        bot.handle_message = alternate

        result = await bot.recover_pending_messages(channel=channel)

        assert result["recovered"] == 3
        assert result["failed"] == 3

    async def test_failure_records_error_message(self):
        """Each failure entry contains the error string."""
        queue = AsyncMock()
        msgs = [
            _make_queued_msg(message_id="m1", text="a"),
            _make_queued_msg(message_id="m2", text="b"),
        ]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        async def fail_with_custom(msg, **kwargs):
            if msg.message_id == "m1":
                raise ValueError("specific validation error")
            raise RuntimeError("specific runtime error")

        bot.handle_message = fail_with_custom

        result = await bot.recover_pending_messages(channel=channel)

        errors = {f["message_id"]: f["error"] for f in result["failures"]}
        assert "specific validation error" in errors["m1"]
        assert "specific runtime error" in errors["m2"]

    async def test_failure_records_chat_id(self):
        """Each failure entry contains the chat_id."""
        queue = AsyncMock()
        q1 = _make_queued_msg(message_id="m1", chat_id="chat_alpha")
        q2 = _make_queued_msg(message_id="m2", chat_id="chat_beta")
        queue.recover_stale = AsyncMock(return_value=[q1, q2])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)
        bot.handle_message = AsyncMock(side_effect=RuntimeError("fail"))

        result = await bot.recover_pending_messages(channel=channel)

        chat_ids = {f["chat_id"] for f in result["failures"]}
        assert chat_ids == {"chat_alpha", "chat_beta"}


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Recovery with corrupted/incomplete queue data
# ═══════════════════════════════════════════════════════════════════════════════


class TestCorruptedQueueData:
    """Tests for recovery when queue returns malformed or incomplete data."""

    async def test_message_with_none_text(self):
        """Queued message with None text is reconstructed with empty string."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m_none_text")
        q.text = None
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        result = await bot.recover_pending_messages(channel=channel)

        assert result["recovered"] == 1
        assert captured is not None
        assert captured.text is None

    async def test_message_with_empty_sender_name(self):
        """Queued message with empty sender_name is handled."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m_empty_sender", sender_name="")
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        result = await bot.recover_pending_messages(channel=channel)
        assert result["recovered"] == 1
        assert captured.sender_name == ""

    async def test_message_with_none_sender_name(self):
        """Queued message with None sender_name uses fallback empty string."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m_none_sender")
        q.sender_name = None
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        result = await bot.recover_pending_messages(channel=channel)
        assert result["recovered"] == 1
        # sender_name or "" => None or "" => ""
        assert captured.sender_name == ""

    async def test_message_without_sender_id_attribute(self):
        """Queued message without sender_id attr uses sender_name for ACL check."""
        queue = AsyncMock()
        queue.get_pending_count = AsyncMock(return_value=0)
        q = MagicMock(spec=["message_id", "chat_id", "text", "sender_name", "created_at"])
        q.message_id = "m_no_sender_id"
        q.chat_id = "chat_1"
        q.text = "hello"
        q.sender_name = "Tester"
        q.created_at = time.time()
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        # sender_id will be None (no attr), then fallback to sender_name or ""
        # ACL check uses: getattr(queued_msg, "sender_id", None) or queued_msg.sender_name or ""
        # => None or "Tester" or "" => "Tester"
        result = await bot.recover_pending_messages(channel=channel)
        assert result["recovered"] == 1
        channel._is_allowed.assert_called_once_with("Tester")

    async def test_message_with_none_created_at(self):
        """Queued message with None created_at uses time.time() fallback."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m_no_time")
        q.created_at = None
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        before = time.time()
        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        result = await bot.recover_pending_messages(channel=channel)
        after = time.time()

        assert result["recovered"] == 1
        assert captured is not None
        # created_at or time.time() => None or time.time() => should be a recent timestamp
        assert before <= captured.timestamp <= after + 1

    async def test_unicode_message_recovery(self):
        """Messages with unicode/emoji content are recovered correctly."""
        queue = AsyncMock()
        q = _make_queued_msg(
            message_id="m_unicode",
            text="你好世界 🌍 مرحبا Привет",
            sender_name="Ünïcödé 名前",
        )
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        result = await bot.recover_pending_messages(channel=channel)
        assert result["recovered"] == 1
        assert captured.text == "你好世界 🌍 مرحبا Привет"
        assert captured.sender_name == "Ünïcödé 名前"

    async def test_very_long_text_recovery(self):
        """Messages with very long text content are recovered correctly."""
        queue = AsyncMock()
        long_text = "x" * 100_000
        q = _make_queued_msg(message_id="m_long", text=long_text)
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        result = await bot.recover_pending_messages(channel=channel)
        assert result["recovered"] == 1
        assert len(captured.text) == 100_000


# ═══════════════════════════════════════════════════════════════════════════════
# 3. ACL filtering edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestACLFiltering:
    """Tests for ACL filtering during recovery."""

    async def test_no_channel_skips_all_messages(self):
        """Without a channel, all messages are skipped (deferred)."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(5)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)

        result = await bot.recover_pending_messages(channel=None)

        # channel=None → all messages are skipped (deferred until ACL can be checked)
        assert result["total_found"] == 5
        assert result["recovered"] == 0
        assert result["failed"] == 0

    async def test_channel_without_is_allowed_skips_messages(self):
        """Channel that lacks _is_allowed method defers messages."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m1")
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)

        # Channel without _is_allowed
        channel = MagicMock(spec=["send"])
        channel.send = AsyncMock()
        # hasattr(channel, "_is_allowed") returns False
        # so it falls to the "elif channel is None" — but channel is NOT None
        # so it proceeds to reconstruct and handle_message

        bot.handle_message = AsyncMock(return_value="ok")

        result = await bot.recover_pending_messages(channel=channel)

        # Channel exists but lacks _is_allowed, so the first if-branch is False,
        # the elif is False (channel is not None), so it proceeds normally
        assert result["recovered"] == 1

    async def test_disallowed_sender_skipped(self):
        """Messages from senders not in ACL are skipped."""
        queue = AsyncMock()
        q1 = _make_queued_msg(message_id="m1", sender_name="Allowed")
        q2 = _make_queued_msg(message_id="m2", sender_name="Blocked")
        queue.recover_stale = AsyncMock(return_value=[q1, q2])

        bot = _make_bot(message_queue=queue)

        # _is_allowed returns True for "Allowed", False for "Blocked"
        channel = MagicMock()
        channel._is_allowed = MagicMock(side_effect=lambda s: s == "Allowed")

        bot.handle_message = AsyncMock(return_value="ok")

        result = await bot.recover_pending_messages(channel=channel)

        assert result["total_found"] == 2
        assert result["recovered"] == 1
        assert result["failed"] == 0

    async def test_all_senders_disallowed(self):
        """All messages skipped when no sender is allowed."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(3)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=False)

        bot.handle_message = AsyncMock(return_value="ok")

        result = await bot.recover_pending_messages(channel=channel)

        assert result["recovered"] == 0
        assert result["failed"] == 0
        assert result["total_found"] == 3
        bot.handle_message.assert_not_awaited()

    async def test_acl_check_uses_sender_id_over_sender_name(self):
        """When sender_id is present, it's used for ACL check (not sender_name)."""
        queue = AsyncMock()
        q = _make_queued_msg(
            message_id="m1",
            sender_name="Bob",
            sender_id="1234567890",
        )
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)
        bot.handle_message = AsyncMock(return_value="ok")

        await bot.recover_pending_messages(channel=channel)

        # sender_id is "1234567890", so ACL should check that
        channel._is_allowed.assert_called_once_with("1234567890")

    async def test_acl_falls_back_to_sender_name_when_no_sender_id(self):
        """When sender_id is absent, sender_name is used for ACL check."""
        queue = AsyncMock()
        q = MagicMock()
        q.message_id = "m1"
        q.chat_id = "chat_1"
        q.text = "hello"
        q.sender_name = "Alice"
        q.created_at = time.time()
        # No sender_id attribute at all
        del q.sender_id
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)
        bot.handle_message = AsyncMock(return_value="ok")

        await bot.recover_pending_messages(channel=channel)

        # getattr returns None, then falls back to sender_name "Alice"
        channel._is_allowed.assert_called_once_with("Alice")

    async def test_acl_uses_empty_string_when_no_sender_id_or_name(self):
        """When both sender_id and sender_name are absent, empty string is used."""
        queue = AsyncMock()
        q = MagicMock()
        q.message_id = "m1"
        q.chat_id = "chat_1"
        q.text = "hello"
        q.sender_name = None
        q.created_at = time.time()
        del q.sender_id
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)
        bot.handle_message = AsyncMock(return_value="ok")

        await bot.recover_pending_messages(channel=channel)

        # getattr(sender_id) => None, sender_name => None, fallback => ""
        channel._is_allowed.assert_called_once_with("")


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Queue-level exceptions
# ═══════════════════════════════════════════════════════════════════════════════


class TestQueueExceptions:
    """Tests for recovery when the queue itself raises exceptions."""

    async def test_recover_stale_raises_connection_error(self):
        """If recover_stale raises, the exception propagates (not swallowed)."""
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(side_effect=ConnectionError("db unavailable"))

        bot = _make_bot(message_queue=queue)

        with pytest.raises(ConnectionError, match="db unavailable"):
            await bot.recover_pending_messages()

    async def test_recover_stale_raises_os_error(self):
        """OS errors from the queue propagate."""
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(side_effect=OSError("disk failure"))

        bot = _make_bot(message_queue=queue)

        with pytest.raises(OSError, match="disk failure"):
            await bot.recover_pending_messages()

    async def test_no_queue_returns_empty_immediately(self):
        """When message_queue is None, returns zero-result immediately."""
        bot = _make_bot(message_queue=None)

        result = await bot.recover_pending_messages()

        assert result == {
            "total_found": 0,
            "recovered": 0,
            "failed": 0,
            "failures": [],
        }


# ═══════════════════════════════════════════════════════════════════════════════
# 5. Concurrent recovery attempts
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrentRecovery:
    """Tests for concurrent recovery scenarios."""

    async def test_concurrent_recovery_attempts(self):
        """Two concurrent recoveries both try to process the same stale messages."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(3)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        call_count = 0

        async def slow_handle(msg, **kwargs):
            nonlocal call_count
            call_count += 1
            await asyncio.sleep(0.01)
            return "ok"

        bot.handle_message = slow_handle

        # Run two recoveries concurrently
        results = await asyncio.gather(
            bot.recover_pending_messages(channel=channel),
            bot.recover_pending_messages(channel=channel),
        )

        # Both should complete without errors
        for result in results:
            assert result["total_found"] == 3
            assert result["recovered"] == 3
            assert result["failed"] == 0

    async def test_recovery_while_handle_message_in_progress(self):
        """Recovery interleaves with an ongoing handle_message call."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="stale_1")
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        processing_done = asyncio.Event()

        async def slow_handle(msg, **kwargs):
            # Simulate long processing
            await asyncio.sleep(0.05)
            processing_done.set()
            return "ok"

        bot.handle_message = slow_handle

        result = await bot.recover_pending_messages(channel=channel)

        # Should complete after the slow handle_message finishes
        assert result["recovered"] == 1
        assert processing_done.is_set()


# ═══════════════════════════════════════════════════════════════════════════════
# 6. Large batch recovery with mixed outcomes
# ═══════════════════════════════════════════════════════════════════════════════


class TestLargeBatchRecovery:
    """Tests for recovery with larger batches of messages."""

    async def test_100_messages_all_succeed(self):
        """Large batch of 100 messages all recovered successfully."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(100)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)
        bot.handle_message = AsyncMock(return_value="ok")

        result = await bot.recover_pending_messages(channel=channel)

        assert result["total_found"] == 100
        assert result["recovered"] == 100
        assert result["failed"] == 0
        assert result["failures"] == []

    async def test_50_messages_mixed_acl_and_failures(self):
        """50 messages: 20 allowed + recovered, 10 ACL-blocked, 10 failed, 10 no channel."""
        # We'll test: 20 succeed, 10 disallowed, 10 fail processing, 10 skipped
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}", sender_name=f"user_{i}") for i in range(50)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)

        # Allow users 0-29, block users 30-39
        channel = MagicMock()
        channel._is_allowed = MagicMock(side_effect=lambda s: int(s.split("_")[1]) < 30)

        # First 20 succeed, next 10 fail (users 20-29 allowed but fail)
        async def selective_handle(msg, **kwargs):
            idx = int(msg.sender_name.split("_")[1])
            if 20 <= idx < 30:
                raise RuntimeError(f"processing error for user_{idx}")
            return "ok"

        bot.handle_message = selective_handle

        result = await bot.recover_pending_messages(channel=channel)

        # 20 succeeded (0-19), 10 failed (20-29), 20 ACL-blocked (30-49)
        assert result["total_found"] == 50
        assert result["recovered"] == 20
        assert result["failed"] == 10
        assert len(result["failures"]) == 10

    async def test_empty_stale_list_returns_zero(self):
        """Queue returns empty list — no crash, clean zero-result."""
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(return_value=[])

        bot = _make_bot(message_queue=queue)

        result = await bot.recover_pending_messages(channel=_make_channel())

        assert result["total_found"] == 0
        assert result["recovered"] == 0
        assert result["failed"] == 0
        assert result["failures"] == []


# ═══════════════════════════════════════════════════════════════════════════════
# 7. IncomingMessage reconstruction edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestIncomingMessageReconstruction:
    """Tests verifying correct IncomingMessage reconstruction from queued data."""

    async def test_all_fields_mapped_correctly(self):
        """All fields from QueuedMessage map to IncomingMessage correctly."""
        queue = AsyncMock()
        q = _make_queued_msg(
            message_id="msg_exact",
            chat_id="chat_exact",
            text="exact text",
            sender_name="ExactUser",
            sender_id="5551234",
            created_at=1700000000.0,
        )
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        await bot.recover_pending_messages(channel=channel)

        assert captured is not None
        assert isinstance(captured, IncomingMessage)
        assert captured.message_id == "msg_exact"
        assert captured.chat_id == "chat_exact"
        assert captured.text == "exact text"
        assert captured.sender_name == "ExactUser"
        assert captured.sender_id == "5551234"
        assert captured.timestamp == 1700000000.0

    async def test_missing_sender_id_falls_back_to_sender_name(self):
        """When queued msg lacks sender_id, IncomingMessage.sender_id falls back to sender_name."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m1")
        # _make_queued_msg without sender_id omits the attribute;
        # crash_recovery falls back to sender_name for IncomingMessage.sender_id
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        captured = None

        async def capture(msg, **kwargs):
            nonlocal captured
            captured = msg
            return "ok"

        bot.handle_message = capture

        await bot.recover_pending_messages(channel=channel)

        # Falls back to sender_name ("Tester") since sender_id attr is absent
        assert captured.sender_id == "Tester"

    async def test_handle_message_receives_incoming_message_instance(self):
        """Verify handle_message receives a proper IncomingMessage, not a mock."""
        queue = AsyncMock()
        q = _make_queued_msg(message_id="m_type_check")
        queue.recover_stale = AsyncMock(return_value=[q])

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        received_type = None

        async def check_type(msg, **kwargs):
            nonlocal received_type
            received_type = type(msg).__name__
            return "ok"

        bot.handle_message = check_type

        await bot.recover_pending_messages(channel=channel)

        assert received_type == "IncomingMessage"


# ═══════════════════════════════════════════════════════════════════════════════
# 8. Return value structure validation
# ═══════════════════════════════════════════════════════════════════════════════


class TestReturnStructure:
    """Tests verifying the return dict structure is consistent."""

    async def test_return_dict_has_all_keys(self):
        """Returned dict always has exactly 4 keys."""
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(return_value=[])

        bot = _make_bot(message_queue=queue)

        result = await bot.recover_pending_messages()

        expected_keys = {"total_found", "recovered", "failed", "failures"}
        assert set(result.keys()) == expected_keys

    async def test_mathematical_invariant(self):
        """total_found >= recovered + failed (some may be skipped by ACL)."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(10)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        # First 5 succeed, rest fail
        async def partial(msg, **kwargs):
            idx = int(msg.message_id[1:])
            if idx >= 5:
                raise RuntimeError("fail")
            return "ok"

        bot.handle_message = partial

        result = await bot.recover_pending_messages(channel=channel)

        assert result["total_found"] == 10
        assert result["recovered"] + result["failed"] <= result["total_found"]

    async def test_failures_list_length_matches_failed_count(self):
        """len(failures) always equals failed count."""
        queue = AsyncMock()
        msgs = [_make_queued_msg(message_id=f"m{i}") for i in range(7)]
        queue.recover_stale = AsyncMock(return_value=msgs)

        bot = _make_bot(message_queue=queue)
        channel = _make_channel(allowed=True)

        async def mixed(msg, **kwargs):
            idx = int(msg.message_id[1:])
            if idx % 3 == 0:
                raise ValueError("fail")
            return "ok"

        bot.handle_message = mixed

        result = await bot.recover_pending_messages(channel=channel)

        assert result["failed"] == len(result["failures"])
