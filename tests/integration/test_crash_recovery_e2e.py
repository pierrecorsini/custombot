"""
End-to-end test for the crash recovery pipeline.

Tests the full pipeline through the standalone ``recover_pending_messages``
from ``src.bot.crash_recovery`` using a real ``Bot.handle_message`` as the
message handler callback. This ensures the entire path from queue recovery
through LLM response and persistence is exercised:

  stale messages detected
    → reconstructed as IncomingMessage
    → ACL check via channel
    → handle_message (dedup, rate limit, routing, LLM)
    → response delivery and DB persistence
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import Bot, BotConfig, BotDeps
from src.bot.crash_recovery import recover_pending_messages
from src.channels.base import IncomingMessage
from src.routing import RoutingRule
from tests.helpers.llm_mocks import make_chat_response


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_bot_for_recovery(routing=None, message_queue=None) -> Bot:
    """Create a Bot with mocked dependencies for crash recovery e2e tests.

    Mirrors the ``_make_bot`` helper from ``tests/unit/test_bot.py`` so that
    ``handle_message`` can exercise the full pipeline (routing → LLM → DB).
    """
    cfg = BotConfig(
        max_tool_iterations=10,
        memory_max_history=50,
        system_prompt_prefix="",
    )

    db = AsyncMock()
    db.message_exists = AsyncMock(return_value=False)
    db.upsert_chat = AsyncMock()
    db.upsert_chat_and_save_message = AsyncMock()
    db.save_message = AsyncMock()
    db.save_messages_batch = AsyncMock()
    db.get_history = AsyncMock(return_value=[])
    db.get_generation = MagicMock(return_value=0)

    llm = AsyncMock()

    memory = AsyncMock()
    memory.ensure_workspace = MagicMock(return_value=Path("/tmp/workspace/chat_123"))
    memory.read_memory = AsyncMock(return_value="")
    memory.read_agents_md = AsyncMock(return_value="")

    skills = MagicMock()
    skills.tool_definitions = []
    skills.all = MagicMock(return_value=[])

    dedup = AsyncMock()
    dedup.is_inbound_duplicate = AsyncMock(return_value=False)
    dedup.check_outbound_with_key = MagicMock(return_value=(False, "fake-dedup-key"))
    dedup.check_and_record_request = MagicMock(return_value=False)
    dedup.record_outbound_keyed = MagicMock()

    rate_limiter = MagicMock()
    rate_limiter.check_message_rate = MagicMock(
        return_value=MagicMock(allowed=True, remaining=30, limit_value=30)
    )
    rate_limiter.check_rate_limit = MagicMock(
        return_value=MagicMock(allowed=True, remaining=10, limit_value=10)
    )

    tool_executor = AsyncMock()
    tool_executor.close = MagicMock()

    context_assembler = AsyncMock()
    context_assembler.finalize_turn = MagicMock(side_effect=lambda _cid, text: text)
    context_assembler.update_config = MagicMock()

    return Bot(
        BotDeps(
            config=cfg,
            db=db,
            llm=llm,
            memory=memory,
            skills=skills,
            routing=routing,
            message_queue=message_queue,
            dedup=dedup,
            rate_limiter=rate_limiter,
            tool_executor=tool_executor,
            context_assembler=context_assembler,
        )
    )


def _make_queued_msg(
    message_id: str = "msg-1",
    chat_id: str = "chat-1",
    text: str = "Hello from crash",
    sender_name: str = "Alice",
    sender_id: str = "1234567890",
    created_at: float | None = None,
) -> MagicMock:
    """Create a mock queued message with realistic attributes."""
    msg = MagicMock()
    msg.message_id = message_id
    msg.chat_id = chat_id
    msg.text = text
    msg.sender_name = sender_name
    msg.sender_id = sender_id
    msg.created_at = created_at or (time.time() - 600)
    return msg


def _make_channel(allowed: bool = True) -> MagicMock:
    """Create a mock channel with ``_is_allowed`` for ACL checks."""
    channel = MagicMock()
    channel._is_allowed = MagicMock(return_value=allowed)
    return channel


def _make_routing_rule(showErrors: bool = True) -> RoutingRule:
    """Create a catch-all routing rule."""
    return RoutingRule(
        id="recovery-catch-all",
        priority=100,
        sender="*",
        recipient="*",
        channel="*",
        content_regex="*",
        instruction="chat.agent.md",
        enabled=True,
        showErrors=showErrors,
    )


def _patch_pipeline():
    """Return a context manager that patches the LLM context-building internals.

    Allows ``handle_message`` to run through routing → LLM → delivery
    without needing real file I/O or context assembly.
    """
    import contextlib

    @contextlib.contextmanager
    def _ctx(bot: Bot, *, finalize_side_effect=None):
        effect = finalize_side_effect or (lambda _cid, text: text)
        with (
            patch(
                "src.core.context_assembler.build_context",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(bot._context_assembler, "finalize_turn", side_effect=effect),
            patch.object(bot._instruction_loader, "load", return_value="You are a helpful assistant."),
        ):
            yield

    return _ctx


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Stale messages are reprocessed end-to-end
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleMessagesAreReprocessed:
    """End-to-end: stale messages → reconstructed → LLM called → response persisted."""

    async def test_full_pipeline_with_multiple_stale_messages(self):
        """Multiple stale messages are reconstructed, routed to the LLM,
        and each response is persisted to the database."""
        # Set up mock queue returning stale messages
        queue = AsyncMock()
        q1 = _make_queued_msg(message_id="msg_stale_1", chat_id="chat_a", text="Help me!")
        q2 = _make_queued_msg(message_id="msg_stale_2", chat_id="chat_b", text="What is 2+2?")
        queue.recover_stale = AsyncMock(return_value=[q1, q2])

        # Wire routing so handle_message reaches the LLM
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        channel = _make_channel(allowed=True)

        # LLM returns a distinct response per call
        bot._llm.chat = AsyncMock(
            side_effect=[
                make_chat_response(content="I can help!"),
                make_chat_response(content="2+2 equals 4."),
            ]
        )

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            result = await recover_pending_messages(
                message_queue=queue,
                handle_message=bot.handle_message,
                channel=channel,
            )

        # Recovery stats
        assert result["total_found"] == 2
        assert result["recovered"] == 2
        assert result["failed"] == 0
        assert result["failures"] == []

        # LLM was called once per stale message
        assert bot._llm.chat.await_count == 2

        # Responses were persisted — one batch save per message turn
        assert bot._db.save_messages_batch.await_count == 2

        # Verify the user messages were persisted with original text
        persisted_contents = [
            call.kwargs.get("content", call.args[2] if len(call.args) > 2 else None)
            for call in bot._db.upsert_chat_and_save_message.await_args_list
        ]
        assert "Help me!" in persisted_contents
        assert "What is 2+2?" in persisted_contents

    async def test_single_stale_message_recovery(self):
        """Single stale message traverses the full pipeline."""
        queue = AsyncMock()
        q = _make_queued_msg(
            message_id="msg_single",
            chat_id="chat_single",
            text="Recover me",
            sender_id="user_abc",
        )
        queue.recover_stale = AsyncMock(return_value=[q])

        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        bot._llm.chat = AsyncMock(
            return_value=make_chat_response(content="Recovered successfully!")
        )

        channel = _make_channel(allowed=True)

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            result = await recover_pending_messages(
                message_queue=queue,
                handle_message=bot.handle_message,
                channel=channel,
            )

        assert result["recovered"] == 1
        bot._llm.chat.assert_awaited_once()
        bot._db.save_messages_batch.assert_awaited_once()

    async def test_reconstructed_message_fields_are_correct(self):
        """Fields from the queued message are faithfully reconstructed
        into the IncomingMessage passed to handle_message."""
        queue = AsyncMock()
        q = _make_queued_msg(
            message_id="msg_fields",
            chat_id="chat_fields",
            text="Field check",
            sender_name="Bob",
            sender_id="9876543210",
            created_at=1700000000.0,
        )
        queue.recover_stale = AsyncMock(return_value=[q])

        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        channel = _make_channel(allowed=True)

        captured_msg = None

        # Intercept handle_message to inspect the reconstructed IncomingMessage
        original_handle = bot.handle_message

        async def capture_and_handle(msg, **kwargs):
            nonlocal captured_msg
            captured_msg = msg
            return await original_handle(msg, **kwargs)

        bot._llm.chat = AsyncMock(
            return_value=make_chat_response(content="ok")
        )

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            await recover_pending_messages(
                message_queue=queue,
                handle_message=capture_and_handle,
                channel=channel,
            )

        assert captured_msg is not None
        assert isinstance(captured_msg, IncomingMessage)
        assert captured_msg.message_id == "msg_fields"
        assert captured_msg.chat_id == "chat_fields"
        assert captured_msg.text == "Field check"
        assert captured_msg.sender_name == "Bob"
        assert captured_msg.sender_id == "9876543210"
        assert captured_msg.acl_passed is True


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Stale messages within TTL are NOT reprocessed
# ─────────────────────────────────────────────────────────────────────────────


class TestStaleMessagesSkipExpiredTTL:
    """Messages that are NOT stale (within TTL) are not reprocessed.

    The ``MessageQueue.recover_stale`` method is the TTL gate: it returns
    only messages whose ``updated_at`` is older than the cutoff.  Messages
    still within the TTL window are never passed to the recovery pipeline.
    """

    async def test_fresh_messages_not_recovered(self):
        """When all messages are within the TTL window, recover_stale returns
        an empty list and no LLM calls or DB writes occur."""
        queue = AsyncMock()
        # Empty list → all messages are fresh (within TTL)
        queue.recover_stale = AsyncMock(return_value=[])

        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)

        result = await recover_pending_messages(
            message_queue=queue,
            handle_message=bot.handle_message,
            channel=_make_channel(allowed=True),
        )

        assert result["total_found"] == 0
        assert result["recovered"] == 0
        assert result["failed"] == 0
        assert result["failures"] == []

        # LLM was never called
        bot._llm.chat.assert_not_called()
        # Nothing was persisted
        bot._db.save_messages_batch.assert_not_called()
        bot._db.upsert_chat_and_save_message.assert_not_called()

    async def test_only_stale_subset_is_reprocessed(self):
        """When some messages are stale and others are fresh,
        only the stale ones returned by ``recover_stale`` are reprocessed."""
        queue = AsyncMock()
        # Only one stale message; the fresh ones are not returned
        stale_msg = _make_queued_msg(message_id="stale_only", text="old message")
        queue.recover_stale = AsyncMock(return_value=[stale_msg])

        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        bot._llm.chat = AsyncMock(
            return_value=make_chat_response(content="Recovered!")
        )

        channel = _make_channel(allowed=True)

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            result = await recover_pending_messages(
                message_queue=queue,
                handle_message=bot.handle_message,
                channel=channel,
            )

        # Only the stale message was recovered
        assert result["total_found"] == 1
        assert result["recovered"] == 1
        bot._llm.chat.assert_awaited_once()

    async def test_custom_timeout_forwarded_to_queue(self):
        """Custom ``timeout_seconds`` is forwarded to ``recover_stale``."""
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(return_value=[])

        await recover_pending_messages(
            message_queue=queue,
            handle_message=AsyncMock(return_value="ok"),
            timeout_seconds=600,
        )

        queue.recover_stale.assert_awaited_once_with(600)

    async def test_default_timeout_passes_none(self):
        """When no timeout is specified, ``None`` is passed (queue uses its default)."""
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(return_value=[])

        await recover_pending_messages(
            message_queue=queue,
            handle_message=AsyncMock(return_value="ok"),
        )

        queue.recover_stale.assert_awaited_once_with(None)


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Partial recovery continues on error
# ─────────────────────────────────────────────────────────────────────────────


class TestPartialRecoveryContinuesOnError:
    """When some messages fail during recovery, the remaining messages
    continue processing.  Errors are recorded in the ``failures`` list
    without aborting the batch.
    """

    async def test_second_message_fails_first_and_third_succeed(self):
        """3 stale messages: 1st succeeds, 2nd fails (LLM error), 3rd succeeds."""
        queue = AsyncMock()
        q1 = _make_queued_msg(message_id="msg_ok_1", chat_id="chat_1", text="First")
        q2 = _make_queued_msg(message_id="msg_fail", chat_id="chat_2", text="Second")
        q3 = _make_queued_msg(message_id="msg_ok_2", chat_id="chat_3", text="Third")
        queue.recover_stale = AsyncMock(return_value=[q1, q2, q3])

        routing = MagicMock()
        rule = _make_routing_rule(showErrors=True)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        channel = _make_channel(allowed=True)

        # LLM: 1st succeeds, 2nd fails, 3rd succeeds
        response_ok = make_chat_response(content="OK")
        bot._llm.chat = AsyncMock(
            side_effect=[
                response_ok,
                RuntimeError("LLM connection error"),
                response_ok,
            ]
        )

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            result = await recover_pending_messages(
                message_queue=queue,
                handle_message=bot.handle_message,
                channel=channel,
            )

        # Recovery stats
        assert result["total_found"] == 3
        assert result["recovered"] == 2
        assert result["failed"] == 1

        # Failure details
        assert len(result["failures"]) == 1
        assert result["failures"][0]["message_id"] == "msg_fail"
        assert result["failures"][0]["chat_id"] == "chat_2"
        assert "LLM connection error" in result["failures"][0]["error"]

        # LLM was attempted for all 3 messages
        assert bot._llm.chat.await_count == 3

        # 2 successful persist batches (1st and 3rd messages)
        assert bot._db.save_messages_batch.await_count == 2

    async def test_multiple_interleaved_failures(self):
        """5 messages: indices 1 and 3 fail, rest succeed — failures are independent."""
        queue = AsyncMock()
        messages = [
            _make_queued_msg(message_id=f"msg_{i}", chat_id=f"chat_{i}", text=f"Text {i}")
            for i in range(5)
        ]
        queue.recover_stale = AsyncMock(return_value=messages)

        routing = MagicMock()
        rule = _make_routing_rule(showErrors=True)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        channel = _make_channel(allowed=True)

        # Fail indices 1 and 3
        responses = []
        for i in range(5):
            if i in (1, 3):
                responses.append(RuntimeError(f"Error on message {i}"))
            else:
                responses.append(make_chat_response(content=f"Response {i}"))

        bot._llm.chat = AsyncMock(side_effect=responses)

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            result = await recover_pending_messages(
                message_queue=queue,
                handle_message=bot.handle_message,
                channel=channel,
            )

        assert result["total_found"] == 5
        assert result["recovered"] == 3
        assert result["failed"] == 2

        failed_ids = {f["message_id"] for f in result["failures"]}
        assert failed_ids == {"msg_1", "msg_3"}

    async def test_all_messages_fail_gracefully(self):
        """All 3 messages fail but recovery completes without raising."""
        queue = AsyncMock()
        messages = [
            _make_queued_msg(message_id=f"msg_{i}", chat_id=f"chat_{i}")
            for i in range(3)
        ]
        queue.recover_stale = AsyncMock(return_value=messages)

        routing = MagicMock()
        rule = _make_routing_rule(showErrors=True)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))

        bot = _make_bot_for_recovery(routing=routing, message_queue=queue)
        bot._llm.chat = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        channel = _make_channel(allowed=True)

        patch_pipeline = _patch_pipeline()
        with patch_pipeline(bot):
            result = await recover_pending_messages(
                message_queue=queue,
                handle_message=bot.handle_message,
                channel=channel,
            )

        assert result["total_found"] == 3
        assert result["recovered"] == 0
        assert result["failed"] == 3
        assert len(result["failures"]) == 3

        for failure in result["failures"]:
            assert "LLM unavailable" in failure["error"]
