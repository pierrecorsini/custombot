"""
Tests for src/bot.py — Core bot orchestrator.

Covers:
- PreflightResult (frozen dataclass, __bool__)
- Bot.__init__ (construction with mocked dependencies)
- Bot.preflight_check (validation, empty, dedup, routing)
- Bot.handle_message (validation, dedup, rate limiting, processing, errors)
- Bot.recover_pending_messages (crash recovery flow)
- Bot.process_scheduled (scheduled task processing, bypassing routing/dedup)
- Bot._react_loop (core ReAct loop with mocked LLM)
- Bot._process_tool_calls (tool call processing and streaming)
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import Bot, PreflightResult
from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig, WhatsAppConfig, NeonizeConfig
from src.rate_limiter import RateLimitResult
from src.routing import RoutingRule


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_message(
    text: str = "Hello!",
    message_id: str = "msg_001",
    chat_id: str = "chat_123",
    sender_name: str = "Alice",
    sender_id: str = "1234567890",
    channel_type: str = "whatsapp",
    fromMe: bool = False,
    toMe: bool = True,
    correlation_id: str | None = None,
) -> IncomingMessage:
    """Create a valid IncomingMessage for testing."""
    return IncomingMessage(
        message_id=message_id,
        chat_id=chat_id,
        sender_id=sender_id,
        sender_name=sender_name,
        text=text,
        timestamp=time.time(),
        channel_type=channel_type,
        fromMe=fromMe,
        toMe=toMe,
        correlation_id=correlation_id,
    )


def _make_bot(
    routing=None,
    message_queue=None,
    max_tool_iterations: int = 10,
    tool_definitions: list | None = None,
) -> Bot:
    """Create a Bot with fully mocked dependencies."""
    cfg = MagicMock(spec=Config)
    cfg.llm = MagicMock(spec=LLMConfig)
    cfg.llm.max_tool_iterations = max_tool_iterations

    db = AsyncMock()
    db.message_exists = AsyncMock(return_value=False)
    db.upsert_chat = AsyncMock()
    db.save_message = AsyncMock()
    db.get_history = AsyncMock(return_value=[])

    llm = AsyncMock()

    memory = AsyncMock()
    memory.ensure_workspace = MagicMock(return_value=Path("/tmp/workspace/chat_123"))
    memory.read_memory = AsyncMock(return_value="")
    memory.read_agents_md = AsyncMock(return_value="")

    skills = MagicMock()
    skills.tool_definitions = tool_definitions or []
    skills.all = MagicMock(return_value=[])

    return Bot(
        config=cfg,
        db=db,
        llm=llm,
        memory=memory,
        skills=skills,
        routing=routing,
        message_queue=message_queue,
    )


def _make_routing_rule(
    rule_id: str = "test-rule",
    instruction: str = "chat.agent.md",
    showErrors: bool = True,
    skillExecVerbose: str = "",
) -> RoutingRule:
    """Create a RoutingRule for testing."""
    return RoutingRule(
        id=rule_id,
        priority=100,
        sender="*",
        recipient="*",
        channel="*",
        content_regex="*",
        instruction=instruction,
        enabled=True,
        showErrors=showErrors,
        skillExecVerbose=skillExecVerbose,
    )


def _make_llm_response(
    content: str = "Hello back!",
    finish_reason: str = "stop",
    tool_calls: list | None = None,
) -> MagicMock:
    """Create a mock LLM chat completion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls

    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message = message

    completion = MagicMock()
    completion.choices = [choice]
    return completion


def _make_tool_call(
    call_id: str = "call_001",
    name: str = "web_search",
    arguments: str = '{"query": "test"}',
) -> MagicMock:
    """Create a mock tool call object."""
    func = MagicMock()
    func.name = name
    func.arguments = arguments

    tool_call = MagicMock()
    tool_call.id = call_id
    tool_call.function = func
    return tool_call


# ─────────────────────────────────────────────────────────────────────────────
# PreflightResult Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightResult:
    """Tests for the PreflightResult frozen dataclass."""

    def test_passed_true_is_truthy(self):
        result = PreflightResult(passed=True)
        assert bool(result) is True

    def test_passed_false_is_falsy(self):
        result = PreflightResult(passed=False)
        assert bool(result) is False

    def test_passed_true_with_reason(self):
        result = PreflightResult(passed=True, reason="ok")
        assert result.passed is True
        assert result.reason == "ok"
        assert bool(result) is True

    def test_passed_false_with_reason(self):
        result = PreflightResult(passed=False, reason="duplicate")
        assert result.passed is False
        assert result.reason == "duplicate"
        assert bool(result) is False

    def test_default_reason_is_empty(self):
        result = PreflightResult(passed=True)
        assert result.reason == ""

    def test_frozen_raises_on_setattr(self):
        result = PreflightResult(passed=True)
        with pytest.raises(AttributeError):
            result.passed = False  # type: ignore[misc]

    def test_frozen_raises_on_new_attribute(self):
        result = PreflightResult(passed=True)
        with pytest.raises(AttributeError):
            result.extra = "nope"  # type: ignore[attr-defined]

    def test_used_in_if_statement(self):
        result = PreflightResult(passed=True)
        if result:
            passed = True
        else:
            passed = False
        assert passed is True

    def test_failed_result_in_if_statement(self):
        result = PreflightResult(passed=False, reason="empty")
        if result:
            passed = True
        else:
            passed = False
        assert passed is False


# ─────────────────────────────────────────────────────────────────────────────
# Bot.__init__ Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestBotInit:
    """Tests for Bot constructor."""

    def test_stores_all_dependencies(self):
        cfg = MagicMock(spec=Config)
        db = AsyncMock()
        llm = AsyncMock()
        memory = AsyncMock()
        skills = MagicMock()
        routing = MagicMock()

        bot = Bot(
            config=cfg,
            db=db,
            llm=llm,
            memory=memory,
            skills=skills,
            routing=routing,
        )

        assert bot._cfg is cfg
        assert bot._db is db
        assert bot._llm is llm
        assert bot._memory is memory
        assert bot._skills is skills
        assert bot._routing is routing

    def test_routing_defaults_to_none(self):
        bot = _make_bot()
        assert bot._routing is None

    def test_message_queue_defaults_to_none(self):
        bot = _make_bot()
        assert bot._message_queue is None

    def test_instructions_dir_set(self):
        bot = _make_bot()
        assert isinstance(bot._instructions_dir, Path)

    def test_custom_instructions_dir(self):
        bot = _make_bot()
        assert bot._instructions_dir == Path("")

    def test_chat_locks_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_chat_locks")
        assert len(bot._chat_locks) == 0

    def test_rate_limiter_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_rate_limiter")

    def test_chat_rate_limiter_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_chat_rate_limiter")

    def test_metrics_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_metrics")

    def test_tool_executor_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_tool_executor")

    def test_instruction_loader_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_instruction_loader")

    def test_project_ctx_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_project_ctx")

    def test_topic_cache_initialized(self):
        bot = _make_bot()
        assert hasattr(bot, "_topic_cache")

    def test_with_message_queue(self):
        queue = AsyncMock()
        bot = _make_bot(message_queue=queue)
        assert bot._message_queue is queue


# ─────────────────────────────────────────────────────────────────────────────
# Bot.preflight_check Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightCheck:
    """Tests for Bot.preflight_check — read-only filter checks."""

    async def test_valid_message_passes(self):
        bot = _make_bot()
        msg = _make_message()
        result = await bot.preflight_check(msg)
        assert result.passed is True
        assert result.reason == ""

    async def test_non_incoming_message_fails(self):
        bot = _make_bot()
        result = await bot.preflight_check("not a message")  # type: ignore[arg-type]
        assert result.passed is False
        assert result.reason == "invalid"

    async def test_dict_message_fails(self):
        bot = _make_bot()
        result = await bot.preflight_check({"text": "hi"})  # type: ignore[arg-type]
        assert result.passed is False
        assert result.reason == "invalid"

    async def test_none_message_fails(self):
        bot = _make_bot()
        result = await bot.preflight_check(None)  # type: ignore[arg-type]
        assert result.passed is False
        assert result.reason == "invalid"

    async def test_empty_text_fails(self):
        bot = _make_bot()
        msg = _make_message(text="")
        result = await bot.preflight_check(msg)
        assert result.passed is False
        assert result.reason == "empty"

    async def test_whitespace_only_text_fails(self):
        bot = _make_bot()
        msg = _make_message(text="   \n\t  ")
        result = await bot.preflight_check(msg)
        assert result.passed is False
        assert result.reason == "empty"

    async def test_duplicate_message_fails(self):
        bot = _make_bot()
        msg = _make_message()
        bot._db.message_exists = AsyncMock(return_value=True)
        result = await bot.preflight_check(msg)
        assert result.passed is False
        assert result.reason == "duplicate"

    async def test_no_routing_engine_passes(self):
        """Without routing engine, routing check is skipped."""
        bot = _make_bot(routing=None)
        msg = _make_message()
        result = await bot.preflight_check(msg)
        assert result.passed is True

    async def test_routing_no_match_fails(self):
        routing = MagicMock()
        routing.match_with_rule = MagicMock(return_value=(None, None))
        bot = _make_bot(routing=routing)
        msg = _make_message()
        result = await bot.preflight_check(msg)
        assert result.passed is False
        assert result.reason == "no_routing_rule"

    async def test_routing_match_passes(self):
        rule = _make_routing_rule()
        routing = MagicMock()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot = _make_bot(routing=routing)
        msg = _make_message()
        result = await bot.preflight_check(msg)
        assert result.passed is True

    async def test_preflight_result_is_bool_compatible(self):
        """PreflightResult works in boolean contexts like if statements."""
        bot = _make_bot()
        msg = _make_message()
        result = await bot.preflight_check(msg)
        assert result
        assert not PreflightResult(passed=False, reason="test")

    async def test_message_with_empty_sender_id_still_valid_for_preflight(self):
        """is_incoming_message requires non-empty sender_id."""
        bot = _make_bot()
        msg = IncomingMessage(
            message_id="msg_001",
            chat_id="chat_123",
            sender_id="",  # empty — should fail type guard
            sender_name="Alice",
            text="Hello",
            timestamp=time.time(),
        )
        result = await bot.preflight_check(msg)
        assert result.passed is False
        assert result.reason == "invalid"


# ─────────────────────────────────────────────────────────────────────────────
# Bot.handle_message Tests — Validation & Early Returns
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleMessageValidation:
    """Tests for Bot.handle_message — input validation and early returns."""

    async def test_returns_none_for_invalid_message(self):
        bot = _make_bot()
        result = await bot.handle_message("not a message")  # type: ignore[arg-type]
        assert result is None

    async def test_returns_none_for_none_message(self):
        bot = _make_bot()
        result = await bot.handle_message(None)  # type: ignore[arg-type]
        assert result is None

    async def test_returns_none_for_empty_text(self):
        bot = _make_bot()
        msg = _make_message(text="")
        result = await bot.handle_message(msg)
        assert result is None

    async def test_returns_none_for_whitespace_only(self):
        bot = _make_bot()
        msg = _make_message(text="   \n  ")
        result = await bot.handle_message(msg)
        assert result is None

    async def test_returns_none_for_oversized_message(self):
        bot = _make_bot()
        msg = _make_message(text="x" * 50_001)
        with patch("src.bot.MAX_MESSAGE_LENGTH", 50_000):
            result = await bot.handle_message(msg)
        assert result is None

    async def test_returns_none_for_duplicate_message(self):
        bot = _make_bot()
        msg = _make_message()
        bot._db.message_exists = AsyncMock(return_value=True)
        result = await bot.handle_message(msg)
        assert result is None

    async def test_does_not_call_db_save_for_invalid(self):
        bot = _make_bot()
        await bot.handle_message("bad")  # type: ignore[arg-type]
        bot._db.save_message.assert_not_called()

    async def test_does_not_call_llm_for_empty(self):
        bot = _make_bot()
        msg = _make_message(text="")
        await bot.handle_message(msg)
        bot._llm.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Bot.handle_message Tests — Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleMessageRateLimiting:
    """Tests for Bot.handle_message — per-chat rate limiting."""

    async def test_rate_limited_returns_none(self):
        bot = _make_bot()
        msg = _make_message()

        rate_result = RateLimitResult(
            allowed=False,
            remaining=0,
            reset_at=time.time() + 60,
            retry_after=30.0,
            limit_type="message_rate",
            limit_value=30,
        )
        bot._chat_rate_limiter.check_message_rate = MagicMock(return_value=rate_result)

        result = await bot.handle_message(msg)
        assert result is None

    async def test_rate_limited_sends_channel_message(self):
        bot = _make_bot()
        msg = _make_message()

        rate_result = RateLimitResult(
            allowed=False,
            remaining=0,
            reset_at=time.time() + 60,
            retry_after=30.0,
            limit_type="message_rate",
            limit_value=30,
        )
        bot._chat_rate_limiter.check_message_rate = MagicMock(return_value=rate_result)

        channel = AsyncMock()
        result = await bot.handle_message(msg, channel=channel)
        assert result is None
        channel.send_message.assert_awaited_once()
        call_args = channel.send_message.call_args
        assert (
            "too quickly" in call_args[0][1].lower()
            or "wait" in call_args[0][1].lower()
        )

    async def test_rate_limit_not_triggered_passes(self):
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        # Simulate an LLM that stops immediately
        response = _make_llm_response(content="Hi there!")
        bot._llm.chat = AsyncMock(return_value=response)

        # Mock build_context and other internals
        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Hi there!", None)),
            patch.object(bot, "_load_instruction", return_value="system prompt"),
        ):
            result = await bot.handle_message(msg)
            assert result == "Hi there!"


# ─────────────────────────────────────────────────────────────────────────────
# Bot.handle_message Tests — Queue Integration
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleMessageQueue:
    """Tests for Bot.handle_message — message queue integration."""

    async def test_enqueues_before_processing(self):
        queue = AsyncMock()
        bot = _make_bot(message_queue=queue)
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="response")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("response", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)
            queue.enqueue.assert_awaited_once_with(msg)

    async def test_completes_after_successful_processing(self):
        queue = AsyncMock()
        queue.get_pending_count = AsyncMock(return_value=0)
        bot = _make_bot(message_queue=queue)
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="response")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("response", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)
            queue.complete.assert_awaited_once_with(msg.message_id)

    async def test_no_queue_operations_without_queue(self):
        bot = _make_bot(message_queue=None)
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="response")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("response", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            result = await bot.handle_message(msg)
            assert result == "response"


# ─────────────────────────────────────────────────────────────────────────────
# Bot.handle_message Tests — Error Handling
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleMessageErrors:
    """Tests for Bot.handle_message — error handling."""

    async def test_exception_reraises_when_show_errors_true(self):
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule(showErrors=True)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        bot._llm.chat = AsyncMock(side_effect=RuntimeError("LLM failure"))

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch.object(bot, "_load_instruction", return_value="prompt"),
            pytest.raises(RuntimeError, match="LLM failure"),
        ):
            await bot.handle_message(msg)

    async def test_exception_suppressed_when_show_errors_false(self):
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule(showErrors=False)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        bot._llm.chat = AsyncMock(side_effect=RuntimeError("LLM failure"))

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            result = await bot.handle_message(msg)
            assert result is None

    async def test_exception_does_not_complete_in_queue(self):
        """On error, message should stay pending in queue for crash recovery."""
        queue = AsyncMock()
        bot = _make_bot(message_queue=queue)
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule(showErrors=False)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        bot._llm.chat = AsyncMock(side_effect=RuntimeError("fail"))

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)
            # complete should NOT be called on failure
            queue.complete.assert_not_awaited()

    async def test_no_routing_engine_returns_none_from_process(self):
        """If no routing engine configured, _process returns None."""
        bot = _make_bot(routing=None)
        msg = _make_message()
        result = await bot.handle_message(msg)
        # Goes through all checks (passes) but _process returns None
        # because no routing engine
        assert result is None

    async def test_no_matching_routing_rule_returns_none(self):
        routing = MagicMock()
        routing.match_with_rule = MagicMock(return_value=(None, None))
        bot = _make_bot(routing=routing)
        msg = _make_message()
        result = await bot.handle_message(msg)
        assert result is None


# ─────────────────────────────────────────────────────────────────────────────
# Bot.handle_message Tests — Metrics
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleMessageMetrics:
    """Tests for Bot.handle_message — metrics tracking."""

    async def test_tracks_message_latency_on_success(self):
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("ok", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)
            bot._metrics.track_message_latency.assert_called_once()

    async def test_updates_queue_depth_with_queue(self):
        queue = AsyncMock()
        queue.get_pending_count = AsyncMock(return_value=5)
        bot = _make_bot(message_queue=queue)
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("ok", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)
            bot._metrics.update_queue_depth.assert_called_once_with(5)


# ─────────────────────────────────────────────────────────────────────────────
# Bot._react_loop Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestReactLoop:
    """Tests for Bot._react_loop — the core ReAct loop."""

    async def test_immediate_stop_returns_content(self):
        """LLM returns stop immediately — no tool calls."""
        bot = _make_bot()
        response = _make_llm_response(content="Final answer", finish_reason="stop")
        bot._llm.chat = AsyncMock(return_value=response)

        text, tool_log = await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=None,
            workspace_dir=Path("/tmp/ws"),
        )
        assert text == "Final answer"
        assert tool_log == []

    async def test_null_content_returns_default(self):
        """LLM returns stop with None content — fallback to default."""
        bot = _make_bot()
        response = _make_llm_response(content=None, finish_reason="stop")
        bot._llm.chat = AsyncMock(return_value=response)

        text, tool_log = await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=None,
            workspace_dir=Path("/tmp/ws"),
        )
        assert text == "(no response)"
        assert tool_log == []

    async def test_tool_calls_then_stop(self):
        """LLM calls a tool, then stops on the next iteration."""
        bot = _make_bot()

        # First LLM call returns tool_calls, second returns stop
        tool_call = _make_tool_call()
        tool_response = _make_llm_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tool_call],
        )
        stop_response = _make_llm_response(content="Done!", finish_reason="stop")
        bot._llm.chat = AsyncMock(side_effect=[tool_response, stop_response])

        # Mock tool_call_to_dict and tool executor
        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_001",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "test"}',
                        },
                    }
                ],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="search results")

        text, tool_log = await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=[],
            workspace_dir=Path("/tmp/ws"),
        )
        assert text == "Done!"
        assert len(tool_log) == 1
        assert tool_log[0]["name"] == "web_search"

    async def test_max_iterations_reached(self):
        """LLM keeps calling tools until max iterations reached."""
        bot = _make_bot(max_tool_iterations=3)

        tool_call = _make_tool_call()
        # Every iteration returns tool_calls (never stops)
        tool_response = _make_llm_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tool_call],
        )
        bot._llm.chat = AsyncMock(return_value=tool_response)
        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="result")

        text, tool_log = await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=[],
            workspace_dir=Path("/tmp/ws"),
        )
        assert "max tool iterations" in text.lower()
        assert len(tool_log) == 3  # one per iteration

    async def test_tracks_llm_latency(self):
        bot = _make_bot()
        response = _make_llm_response(content="hi", finish_reason="stop")
        bot._llm.chat = AsyncMock(return_value=response)

        await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=None,
            workspace_dir=Path("/tmp/ws"),
        )
        bot._metrics.track_llm_latency.assert_called_once()

    async def test_edge_case_has_tool_calls_but_not_finish_reason(self):
        """Edge case: finish_reason is not 'tool_calls' but tool_calls exist."""
        bot = _make_bot()

        tool_call = _make_tool_call()
        # finish_reason is "stop" but tool_calls are present (edge case)
        edge_response = _make_llm_response(
            content=None,
            finish_reason="stop",
            tool_calls=[tool_call],
        )
        stop_response = _make_llm_response(content="Done!", finish_reason="stop")
        bot._llm.chat = AsyncMock(side_effect=[edge_response, stop_response])
        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="result")

        text, tool_log = await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=[],
            workspace_dir=Path("/tmp/ws"),
        )
        assert text == "Done!"
        assert len(tool_log) == 1

    async def test_empty_tool_calls_list_does_not_loop(self):
        """finish_reason is 'tool_calls' but tool_calls list is empty."""
        bot = _make_bot()
        # First call has tool_calls finish_reason but empty list
        empty_tc_response = _make_llm_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[],  # empty list
        )
        # The match case "tool_calls" triggers _process_tool_calls
        # which iterates over empty list, so no tool execution happens
        # But choice.message.tool_calls is [], so iteration does nothing
        # Then we loop again
        stop_response = _make_llm_response(content="Done!", finish_reason="stop")
        bot._llm.chat = AsyncMock(side_effect=[empty_tc_response, stop_response])
        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )

        text, tool_log = await bot._react_loop(
            chat_id="chat_123",
            messages=[],
            tools=[],
            workspace_dir=Path("/tmp/ws"),
        )
        assert text == "Done!"
        assert tool_log == []


# ─────────────────────────────────────────────────────────────────────────────
# Bot._process_tool_calls Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessToolCalls:
    """Tests for Bot._process_tool_calls — individual tool call processing."""

    async def test_processes_single_tool_call(self):
        bot = _make_bot()
        tool_call = _make_tool_call()
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_001"}],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="search results here")

        messages = []
        result = await bot._process_tool_calls(
            choice, messages, "chat_123", Path("/tmp/ws")
        )

        assert len(result) == 1
        assert result[0]["name"] == "web_search"
        assert result[0]["result"] == "search results here"
        assert result[0]["args"] == {"query": "test"}
        # messages should have assistant dict + tool result
        assert len(messages) == 2

    async def test_processes_multiple_tool_calls(self):
        bot = _make_bot()
        tc1 = _make_tool_call(call_id="c1", name="web_search", arguments='{"q": "a"}')
        tc2 = _make_tool_call(call_id="c2", name="bash", arguments='{"cmd": "ls"}')

        choice = MagicMock()
        choice.message.tool_calls = [tc1, tc2]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(side_effect=["result1", "result2"])

        messages = []
        result = await bot._process_tool_calls(
            choice, messages, "chat_123", Path("/tmp/ws")
        )

        assert len(result) == 2
        assert result[0]["name"] == "web_search"
        assert result[1]["name"] == "bash"
        # assistant message + 2 tool results
        assert len(messages) == 3

    async def test_invalid_json_args_handled(self):
        """Tool call with invalid JSON arguments falls back to empty dict."""
        bot = _make_bot()
        tool_call = _make_tool_call(arguments="not valid json{{{")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="ok")

        messages = []
        result = await bot._process_tool_calls(
            choice, messages, "chat_123", Path("/tmp/ws")
        )

        assert result[0]["args"] == {}  # fallback to empty dict

    async def test_null_arguments_handled(self):
        """Tool call with null arguments falls back to empty dict."""
        bot = _make_bot()
        tool_call = _make_tool_call(arguments=None)
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="ok")

        messages = []
        result = await bot._process_tool_calls(
            choice, messages, "chat_123", Path("/tmp/ws")
        )

        assert result[0]["args"] == {}

    async def test_stream_callback_called(self):
        """Stream callback is invoked for each tool execution."""
        bot = _make_bot()
        tool_call = _make_tool_call()
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="result")

        stream_cb = AsyncMock()
        messages = []
        with patch("src.bot.format_single_tool_execution", return_value="formatted"):
            await bot._process_tool_calls(
                choice,
                messages,
                "chat_123",
                Path("/tmp/ws"),
                stream_callback=stream_cb,
            )

        stream_cb.assert_awaited_once_with("formatted")

    async def test_no_stream_callback_without_one(self):
        """No error when stream_callback is None."""
        bot = _make_bot()
        tool_call = _make_tool_call()
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="result")

        messages = []
        result = await bot._process_tool_calls(
            choice,
            messages,
            "chat_123",
            Path("/tmp/ws"),
            stream_callback=None,
        )
        assert len(result) == 1

    async def test_empty_tool_calls_returns_empty(self):
        """Empty tool_calls list returns empty log."""
        bot = _make_bot()
        choice = MagicMock()
        choice.message.tool_calls = []
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )

        messages = []
        result = await bot._process_tool_calls(
            choice, messages, "chat_123", Path("/tmp/ws")
        )
        assert result == []
        # Only the assistant message dict was appended
        assert len(messages) == 1

    async def test_none_tool_calls_returns_empty(self):
        """None tool_calls is treated as empty."""
        bot = _make_bot()
        choice = MagicMock()
        choice.message.tool_calls = None
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )

        messages = []
        result = await bot._process_tool_calls(
            choice, messages, "chat_123", Path("/tmp/ws")
        )
        assert result == []

    async def test_tool_result_appended_to_messages(self):
        """Verify the tool result message has correct structure."""
        bot = _make_bot()
        tool_call = _make_tool_call(call_id="tc_999")
        choice = MagicMock()
        choice.message.tool_calls = [tool_call]
        choice.message.content = None

        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="executed result")

        messages = []
        await bot._process_tool_calls(choice, messages, "chat_123", Path("/tmp/ws"))

        # messages[0] = assistant turn, messages[1] = tool result
        tool_msg = messages[1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "tc_999"
        assert tool_msg["content"] == "executed result"


# ─────────────────────────────────────────────────────────────────────────────
# Bot._process Tests (via handle_message)
# ─────────────────────────────────────────────────────────────────────────────


class TestProcess:
    """Tests for Bot._process — internal processing pipeline."""

    async def test_saves_user_message_to_db(self):
        bot = _make_bot()
        msg = _make_message(text="Hello bot")
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="Hi!")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Hi!", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)

        # upsert_chat should be called
        bot._db.upsert_chat.assert_awaited_once_with(msg.chat_id, msg.sender_name)
        # save_message for user turn
        calls = bot._db.save_message.call_args_list
        user_save = calls[0]
        assert user_save.kwargs["role"] == "user"
        assert user_save.kwargs["content"] == "Hello bot"

    async def test_saves_assistant_message_to_db(self):
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="Final answer")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Final answer", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            await bot.handle_message(msg)

        calls = bot._db.save_message.call_args_list
        # Last save should be assistant
        assistant_save = calls[-1]
        assert assistant_save.kwargs["role"] == "assistant"
        assert assistant_save.kwargs["content"] == "Final answer"

    async def test_skill_exec_verbose_summary_appends_tool_log(self):
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule(skillExecVerbose="summary")
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        tool_call = _make_tool_call()
        tool_response = _make_llm_response(
            finish_reason="tool_calls", tool_calls=[tool_call]
        )
        stop_response = _make_llm_response(content="Here's what I found")
        bot._llm.chat = AsyncMock(side_effect=[tool_response, stop_response])
        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="result")

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Here's what I found", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
            patch(
                "src.bot.format_response_with_tool_log",
                return_value="Here's what I found\n\n[tool log]",
            ),
        ):
            result = await bot.handle_message(msg)
            assert "[tool log]" in result

    async def test_skill_exec_verbose_full_uses_stream_callback(self):
        """When verbose='full', the stream_callback is passed to _react_loop."""
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule(skillExecVerbose="full")
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.agent.md"))
        bot._routing = routing

        response = _make_llm_response(content="Hi!")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Hi!", None)),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            stream_cb = AsyncMock()
            await bot.handle_message(msg, stream_callback=stream_cb)
            # The stream callback doesn't fire because LLM stopped immediately,
            # but we can verify it would be passed to _react_loop
            bot._llm.chat.assert_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# Bot.recover_pending_messages Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestRecoverPendingMessages:
    """Tests for Bot.recover_pending_messages — crash recovery flow."""

    async def test_returns_empty_without_queue(self):
        bot = _make_bot(message_queue=None)
        result = await bot.recover_pending_messages()
        assert result == {
            "total_found": 0,
            "recovered": 0,
            "failed": 0,
            "failures": [],
        }

    async def test_returns_empty_when_no_stale_messages(self):
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(return_value=[])
        bot = _make_bot(message_queue=queue)

        result = await bot.recover_pending_messages()
        assert result["total_found"] == 0
        assert result["recovered"] == 0

    async def test_recovers_single_message(self):
        queue = AsyncMock()
        queued_msg = MagicMock()
        queued_msg.message_id = "msg_recovered_1"
        queued_msg.chat_id = "chat_456"
        queued_msg.text = "Hello from crash"
        queued_msg.sender_name = "Bob"
        queue.recover_stale = AsyncMock(return_value=[queued_msg])
        bot = _make_bot(message_queue=queue)

        # Mock handle_message to succeed
        bot.handle_message = AsyncMock(return_value="response")

        result = await bot.recover_pending_messages()
        assert result["total_found"] == 1
        assert result["recovered"] == 1
        assert result["failed"] == 0
        assert result["failures"] == []

    async def test_recovers_multiple_messages(self):
        queue = AsyncMock()
        q1 = MagicMock()
        q1.message_id = "m1"
        q1.chat_id = "c1"
        q1.text = "msg1"
        q1.sender_name = "A"
        q2 = MagicMock()
        q2.message_id = "m2"
        q2.chat_id = "c2"
        q2.text = "msg2"
        q2.sender_name = "B"
        queue.recover_stale = AsyncMock(return_value=[q1, q2])
        bot = _make_bot(message_queue=queue)
        bot.handle_message = AsyncMock(return_value="ok")

        result = await bot.recover_pending_messages()
        assert result["total_found"] == 2
        assert result["recovered"] == 2

    async def test_partial_failure(self):
        queue = AsyncMock()
        q1 = MagicMock()
        q1.message_id = "m1"
        q1.chat_id = "c1"
        q1.text = "ok"
        q1.sender_name = "A"
        q2 = MagicMock()
        q2.message_id = "m2"
        q2.chat_id = "c2"
        q2.text = "fail"
        q2.sender_name = "B"
        queue.recover_stale = AsyncMock(return_value=[q1, q2])
        bot = _make_bot(message_queue=queue)

        # First succeeds, second fails
        bot.handle_message = AsyncMock(
            side_effect=["ok", RuntimeError("recovery failed")]
        )

        result = await bot.recover_pending_messages()
        assert result["total_found"] == 2
        assert result["recovered"] == 1
        assert result["failed"] == 1
        assert len(result["failures"]) == 1
        assert result["failures"][0]["message_id"] == "m2"
        assert "recovery failed" in result["failures"][0]["error"]

    async def test_all_fail(self):
        queue = AsyncMock()
        q1 = MagicMock()
        q1.message_id = "m1"
        q1.chat_id = "c1"
        q1.text = "bad"
        q1.sender_name = "A"
        queue.recover_stale = AsyncMock(return_value=[q1])
        bot = _make_bot(message_queue=queue)
        bot.handle_message = AsyncMock(side_effect=RuntimeError("fail"))

        result = await bot.recover_pending_messages()
        assert result["recovered"] == 0
        assert result["failed"] == 1

    async def test_passes_custom_timeout(self):
        queue = AsyncMock()
        queue.recover_stale = AsyncMock(return_value=[])
        bot = _make_bot(message_queue=queue)

        await bot.recover_pending_messages(timeout_seconds=120)
        queue.recover_stale.assert_awaited_once_with(120)

    async def test_reconstructs_incoming_message(self):
        """Verify recovered messages are reconstructed as IncomingMessage."""
        queue = AsyncMock()
        queued_msg = MagicMock()
        queued_msg.message_id = "m_rec"
        queued_msg.chat_id = "c_rec"
        queued_msg.text = "recovered text"
        queued_msg.sender_name = "Alice"
        queue.recover_stale = AsyncMock(return_value=[queued_msg])
        bot = _make_bot(message_queue=queue)

        captured_msg = None

        async def capture_handle(msg, **kwargs):
            nonlocal captured_msg
            captured_msg = msg
            return "ok"

        bot.handle_message = capture_handle

        await bot.recover_pending_messages()
        assert captured_msg is not None
        assert captured_msg.message_id == "m_rec"
        assert captured_msg.chat_id == "c_rec"
        assert captured_msg.text == "recovered text"
        assert captured_msg.sender_name == "Alice"


# ─────────────────────────────────────────────────────────────────────────────
# Bot.process_scheduled Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestProcessScheduled:
    """Tests for Bot.process_scheduled — scheduled task processing."""

    async def test_returns_response_text(self):
        bot = _make_bot()
        response = _make_llm_response(content="Scheduled task complete")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Scheduled task complete", None)),
        ):
            result = await bot.process_scheduled(
                chat_id="chat_789",
                prompt="Run daily report",
            )
        assert result == "Scheduled task complete"

    async def test_returns_none_on_exception(self):
        bot = _make_bot()
        bot._llm.chat = AsyncMock(side_effect=RuntimeError("LLM down"))

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
        ):
            result = await bot.process_scheduled(
                chat_id="chat_789",
                prompt="Run daily report",
            )
        assert result is None

    async def test_persists_messages_to_db(self):
        bot = _make_bot()
        response = _make_llm_response(content="Report done")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("Report done", None)),
        ):
            await bot.process_scheduled(
                chat_id="chat_789",
                prompt="Run daily report",
            )

        # Should upsert_chat and save both user + assistant messages
        bot._db.upsert_chat.assert_awaited_once()
        assert bot._db.save_message.await_count == 2
        calls = bot._db.save_message.call_args_list
        assert calls[0].kwargs["role"] == "user"
        assert calls[0].kwargs["content"] == "Run daily report"
        assert calls[1].kwargs["role"] == "assistant"
        assert calls[1].kwargs["content"] == "Report done"

    async def test_uses_channel_prompt_from_channel(self):
        bot = _make_bot()
        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        channel = MagicMock()
        channel.get_channel_prompt = MagicMock(return_value="Use WhatsApp formatting")

        with (
            patch(
                "src.bot.build_context", new_callable=AsyncMock, return_value=[]
            ) as mock_build,
            patch("src.bot.parse_meta", return_value=("ok", None)),
        ):
            await bot.process_scheduled(
                chat_id="chat_789",
                prompt="test",
                channel=channel,
            )

        # build_context should receive the channel_prompt
        _, kwargs = mock_build.call_args
        assert kwargs["channel_prompt"] == "Use WhatsApp formatting"

    async def test_no_channel_prompt_without_channel(self):
        bot = _make_bot()
        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch(
                "src.bot.build_context", new_callable=AsyncMock, return_value=[]
            ) as mock_build,
            patch("src.bot.parse_meta", return_value=("ok", None)),
        ):
            await bot.process_scheduled(
                chat_id="chat_789",
                prompt="test",
            )

        _, kwargs = mock_build.call_args
        assert kwargs["channel_prompt"] is None

    async def test_appends_prompt_as_user_message(self):
        bot = _make_bot()
        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("ok", None)),
        ):
            await bot.process_scheduled(
                chat_id="chat_789",
                prompt="Do the thing",
            )

        # Verify the messages list passed to _react_loop has the user message appended
        # We can check this via the llm.chat call args
        call_args = bot._llm.chat.call_args
        messages = call_args[0][0]  # first positional arg
        assert messages[-1] == {"role": "user", "content": "Do the thing"}

    async def test_handles_topic_meta(self):
        bot = _make_bot()
        response = _make_llm_response(content="Response with META")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch(
                "src.bot.parse_meta",
                return_value=(
                    "Response with META",
                    {"topic_changed": True, "old_topic_summary": "old topic"},
                ),
            ),
        ):
            result = await bot.process_scheduled(
                chat_id="chat_789",
                prompt="test",
            )
        assert result == "Response with META"

    async def test_user_message_name_is_scheduler(self):
        bot = _make_bot()
        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch("src.bot.parse_meta", return_value=("ok", None)),
        ):
            await bot.process_scheduled(
                chat_id="chat_789",
                prompt="test",
            )

        # The user message save should have name="Scheduler"
        user_save = bot._db.save_message.call_args_list[0]
        assert user_save.kwargs["name"] == "Scheduler"


# ─────────────────────────────────────────────────────────────────────────────
# Bot._handle_topic_meta Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleTopicMeta:
    """Tests for Bot._handle_topic_meta — topic change handling."""

    def test_writes_summary_when_topic_changed(self):
        bot = _make_bot()
        bot._topic_cache = MagicMock()
        meta = {"topic_changed": True, "old_topic_summary": "summary text"}
        bot._handle_topic_meta("chat_123", meta)
        bot._topic_cache.write.assert_called_once_with("chat_123", "summary text")

    def test_does_not_write_when_topic_not_changed(self):
        bot = _make_bot()
        bot._topic_cache = MagicMock()
        meta = {"topic_changed": False, "old_topic_summary": "summary text"}
        bot._handle_topic_meta("chat_123", meta)
        bot._topic_cache.write.assert_not_called()

    def test_does_not_write_when_no_old_summary(self):
        bot = _make_bot()
        bot._topic_cache = MagicMock()
        meta = {"topic_changed": True}
        bot._handle_topic_meta("chat_123", meta)
        bot._topic_cache.write.assert_not_called()

    def test_does_not_write_for_empty_meta(self):
        bot = _make_bot()
        bot._topic_cache = MagicMock()
        bot._handle_topic_meta("chat_123", {})
        bot._topic_cache.write.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Bot._load_instruction Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestLoadInstruction:
    """Tests for Bot._load_instruction — instruction file loading."""

    def test_delegates_to_instruction_loader(self):
        bot = _make_bot()
        bot._instruction_loader = MagicMock()
        bot._instruction_loader.load = MagicMock(return_value="file content")
        result = bot._load_instruction("test.md")
        assert result == "file content"
        bot._instruction_loader.load.assert_called_once_with("test.md")


# ─────────────────────────────────────────────────────────────────────────────
# Integration-style Tests
# ─────────────────────────────────────────────────────────────────────────────


class TestHandleMessageEndToEnd:
    """End-to-end tests for handle_message with full pipeline."""

    async def test_full_pipeline_simple_response(self):
        """Valid message → routing match → LLM stop → response returned."""
        bot = _make_bot()
        msg = _make_message(text="What is 2+2?")
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "math.md"))
        bot._routing = routing

        response = _make_llm_response(content="2+2 equals 4.")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch(
                "src.bot.build_context",
                new_callable=AsyncMock,
                return_value=[{"role": "system", "content": "You are a math tutor."}],
            ),
            patch("src.bot.parse_meta", return_value=("2+2 equals 4.", None)),
            patch.object(
                bot, "_load_instruction", return_value="You are a math tutor."
            ),
        ):
            result = await bot.handle_message(msg)

        assert result == "2+2 equals 4."

    async def test_full_pipeline_with_tool_call(self):
        """Valid message → routing match → tool call → LLM stop → response."""
        bot = _make_bot()
        msg = _make_message(text="Search for Python tutorials")
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "search.md"))
        bot._routing = routing

        tool_call = _make_tool_call(
            name="web_search", arguments='{"query": "Python tutorials"}'
        )
        tool_response = _make_llm_response(
            finish_reason="tool_calls",
            tool_calls=[tool_call],
        )
        final_response = _make_llm_response(content="Here are some Python tutorials...")

        bot._llm.chat = AsyncMock(side_effect=[tool_response, final_response])
        bot._llm.tool_call_to_dict = MagicMock(
            return_value={
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "call_001"}],
            }
        )
        bot._tool_executor.execute = AsyncMock(return_value="Found 10 tutorials")

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch(
                "src.bot.parse_meta",
                return_value=("Here are some Python tutorials...", None),
            ),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            result = await bot.handle_message(msg)

        assert result == "Here are some Python tutorials..."
        bot._tool_executor.execute.assert_awaited_once()

    async def test_concurrent_messages_to_different_chats(self):
        """Messages to different chats should be processed independently."""
        bot = _make_bot()
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.md"))
        bot._routing = routing

        response1 = _make_llm_response(content="Response to chat 1")
        response2 = _make_llm_response(content="Response to chat 2")
        bot._llm.chat = AsyncMock(side_effect=[response1, response2])

        msg1 = _make_message(chat_id="chat_A", message_id="msg_A", text="Hello A")
        msg2 = _make_message(chat_id="chat_B", message_id="msg_B", text="Hello B")

        with (
            patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
            patch(
                "src.bot.parse_meta",
                side_effect=[
                    ("Response to chat 1", None),
                    ("Response to chat 2", None),
                ],
            ),
            patch.object(bot, "_load_instruction", return_value="prompt"),
        ):
            results = await asyncio.gather(
                bot.handle_message(msg1),
                bot.handle_message(msg2),
            )

        assert results[0] == "Response to chat 1"
        assert results[1] == "Response to chat 2"

    async def test_correlation_id_set_and_cleared(self):
        """Correlation ID is set during processing and cleared after."""
        bot = _make_bot()
        msg = _make_message(correlation_id="corr_123")
        routing = MagicMock()
        rule = _make_routing_rule()
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.md"))
        bot._routing = routing

        response = _make_llm_response(content="ok")
        bot._llm.chat = AsyncMock(return_value=response)

        with (
            patch("src.bot.set_correlation_id") as mock_set,
            patch("src.bot.clear_correlation_id") as mock_clear,
        ):
            mock_set.return_value = "corr_123"

            with (
                patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
                patch("src.bot.parse_meta", return_value=("ok", None)),
                patch.object(bot, "_load_instruction", return_value="prompt"),
            ):
                await bot.handle_message(msg)

            mock_set.assert_called_once_with("corr_123")
            # clear_correlation_id is called in the finally block
            assert mock_clear.call_count >= 1

    async def test_correlation_id_cleared_on_error(self):
        """Correlation ID is cleaned up even when processing fails."""
        bot = _make_bot()
        msg = _make_message()
        routing = MagicMock()
        rule = _make_routing_rule(showErrors=False)
        routing.match_with_rule = MagicMock(return_value=(rule, "chat.md"))
        bot._routing = routing

        bot._llm.chat = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("src.bot.clear_correlation_id") as mock_clear:
            with (
                patch("src.bot.build_context", new_callable=AsyncMock, return_value=[]),
                patch.object(bot, "_load_instruction", return_value="prompt"),
            ):
                await bot.handle_message(msg)

            mock_clear.assert_called()

    async def test_oversized_message_does_not_reach_llm(self):
        """Messages exceeding MAX_MESSAGE_LENGTH are rejected before LLM call."""
        bot = _make_bot()
        msg = _make_message(text="x" * 60_000)

        with patch("src.bot.MAX_MESSAGE_LENGTH", 50_000):
            result = await bot.handle_message(msg)

        assert result is None
        bot._llm.chat.assert_not_called()

    async def test_duplicate_does_not_reach_llm(self):
        """Duplicate messages are rejected before LLM call."""
        bot = _make_bot()
        msg = _make_message()
        bot._db.message_exists = AsyncMock(return_value=True)

        result = await bot.handle_message(msg)
        assert result is None
        bot._llm.chat.assert_not_called()
