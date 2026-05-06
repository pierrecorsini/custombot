"""
test_message_pipeline.py — Integration test for the full message pipeline.

Exercises the complete flow end-to-end with real (in-memory) components:

  incoming message
    → preflight check (validation, dedup, routing match)
    → routing match
    → LLM call (mocked at the HTTP level, real LLMClient)
    → tool execution (real SkillRegistry with injected skills)
    → response delivery
    → persistence (real Database, real Memory)

The LLM is mocked at the ``AsyncOpenAI`` transport level so that
``LLMClient`` goes through its full path (retry, circuit-breaker,
token tracking, error classification). All other components —
Database, Memory, RoutingEngine, SkillRegistry, MessageQueue, Bot —
are real instances operating on ``tmp_path``.
"""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import Bot
from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig
from src.db import Database
from src.memory import Memory
from src.message_queue import MessageQueue
from src.routing import RoutingEngine, RoutingRule
from src.skills import SkillRegistry
from src.skills.base import BaseSkill

if TYPE_CHECKING:
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_text_response(text: str) -> MagicMock:
    """Build a mock OpenAI ChatCompletion with a plain-text stop response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].finish_reason = "stop"
    response.choices[0].message.content = text
    response.choices[0].message.tool_calls = None
    response.usage = MagicMock()
    response.usage.prompt_tokens = 10
    response.usage.completion_tokens = len(text) // 4
    response.usage.total_tokens = 10 + len(text) // 4
    return response


def _make_tool_call_response(
    tool_name: str,
    tool_args: dict,
    tool_call_id: str = "call_pipeline_001",
) -> MagicMock:
    """Build a mock OpenAI ChatCompletion that requests a tool call."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].finish_reason = "tool_calls"

    tool_call = MagicMock()
    tool_call.id = tool_call_id
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(tool_args)

    response.choices[0].message.content = None
    response.choices[0].message.tool_calls = [tool_call]
    response.usage = MagicMock()
    response.usage.prompt_tokens = 15
    response.usage.completion_tokens = 10
    response.usage.total_tokens = 25
    return response


def _make_routing_engine(workspace: Path) -> RoutingEngine:
    """Create a RoutingEngine with a single catch-all rule."""
    engine = RoutingEngine(workspace)
    engine._rules = [
        RoutingRule(
            id="integration-catch-all",
            priority=100,
            sender="*",
            recipient="*",
            channel="*",
            content_regex="*",
            instruction="chat.agent.md",
            enabled=True,
        )
    ]
    return engine


class _EchoSkill(BaseSkill):
    """Simple skill that echoes its input for pipeline verification."""

    name = "echo"
    description = "Echo back the provided text"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo"}},
        "required": ["text"],
    }

    async def execute(self, workspace_dir: Path, **kwargs) -> str:
        return f"ECHO: {kwargs.get('text', '')}"


class _AppendSkill(BaseSkill):
    """Skill that appends text to a workspace file, proving workspace isolation."""

    name = "append_note"
    description = "Append a note to the workspace"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "Note content"},
        },
        "required": ["content"],
    }

    async def execute(self, workspace_dir: Path, **kwargs) -> str:
        notes_file = workspace_dir / "notes.txt"
        content = kwargs.get("content", "")
        notes_file.parent.mkdir(parents=True, exist_ok=True)
        notes_file.write_text(
            notes_file.read_text(encoding="utf-8") + content + "\n", encoding="utf-8"
        )
        return f"Appended: {content}"


def _make_bot(
    workspace: Path,
    config: Config,
    llm_create_fn,
    skills: SkillRegistry | None = None,
    with_queue: bool = False,
) -> tuple[Bot, Database, Memory, SkillRegistry, MessageQueue | None]:
    """Wire up a full Bot with real components and a mocked LLM transport."""
    from src.llm import LLMClient

    db = Database(str(workspace / ".data"))
    memory = Memory(str(workspace))
    routing = _make_routing_engine(workspace)
    registry = skills or SkillRegistry()
    queue: MessageQueue | None = None
    if with_queue:
        queue = MessageQueue(str(workspace / ".data"))

    llm = LLMClient(config.llm)

    bot = Bot(
        config=config,
        db=db,
        llm=llm,
        memory=memory,
        skills=registry,
        routing=routing,
        message_queue=queue,
        instructions_dir=str(workspace / "instructions"),
    )
    return bot, db, memory, registry, queue


# ─────────────────────────────────────────────────────────────────────────────
# Test: Full Happy Path
# ─────────────────────────────────────────────────────────────────────────────


class TestFullPipelineHappyPath:
    """End-to-end test: message in → preflight → routing → LLM → response."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, tmp_path: Path) -> None:
        """
        Full pipeline with a simple text response (no tool calls).

        Stages exercised:
          1. preflight_check passes
          2. Routing matches catch-all rule
          3. LLM returns a text response
          4. Response is returned to caller
          5. Messages are persisted in DB
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Hello! How can I help?")
            )

            bot, db, memory, skills, queue = _make_bot(workspace, config, None)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-pipeline-001",
                chat_id="chat-pipeline",
                sender_id="user-1",
                sender_name="Alice",
                text="Hi there!",
                timestamp=1000.0,
            )

            # Stage 1: preflight should pass
            preflight = await bot.preflight_check(msg)
            assert preflight.passed, f"Preflight should pass, got reason: {preflight.reason}"

            # Stage 2-4: full pipeline via handle_message
            response = await bot.handle_message(msg)

        await db.close()

        # Stage 4: response delivered
        assert response is not None, "Bot should return a response"
        assert "Hello" in response

        # Stage 5: messages persisted
        rows = await db.get_recent_messages("chat-pipeline", limit=10)
        assert len(rows) >= 2, "Both user and assistant messages should be persisted"
        roles = [r["role"] for r in rows]
        assert "user" in roles
        assert "assistant" in roles


# ─────────────────────────────────────────────────────────────────────────────
# Test: Pipeline with Tool Execution
# ─────────────────────────────────────────────────────────────────────────────


class TestPipelineWithToolExecution:
    """End-to-end test: message → routing → LLM tool call → skill → LLM final."""

    @pytest.mark.asyncio
    async def test_tool_call_then_final_response(self, tmp_path: Path) -> None:
        """
        Full pipeline where LLM calls a tool, the skill executes,
        then LLM produces a final text response.

        Stages exercised:
          1. preflight_check passes
          2. Routing matches
          3. LLM issues a tool call (echo skill)
          4. Skill executes and returns result
          5. LLM produces final text after seeing tool result
          6. Response includes context from tool execution
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test",
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                max_tool_iterations=5,
            )
        )

        skills = SkillRegistry()
        skills._skills["echo"] = _EchoSkill()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            call_count = [0]
            captured_messages: list[list[dict[str, Any]]] = []

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                call_count[0] += 1
                captured_messages.append(list(kwargs.get("messages", [])))
                if call_count[0] == 1:
                    return _make_tool_call_response("echo", {"text": "hello"})
                return _make_text_response("I echoed: hello")

            mock_client.chat.completions.create = _create

            bot, db, memory, _, queue = _make_bot(workspace, config, None, skills=skills)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-tool-pipeline-001",
                chat_id="chat-tools",
                sender_id="user-1",
                sender_name="Bob",
                text="Echo hello",
                timestamp=1000.0,
            )

            # Stage 1: preflight
            preflight = await bot.preflight_check(msg)
            assert preflight.passed

            # Stage 2-6: full pipeline
            response = await bot.handle_message(msg)

        await db.close()

        assert response is not None
        assert "echoed" in response.lower() or "hello" in response.lower()
        assert call_count[0] == 2, "LLM should be called exactly twice (tool + final)"

        # Verify the second LLM call included the tool result in context
        second_call_msgs = captured_messages[1]
        tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1, "Second call should contain one tool result"
        assert "ECHO: hello" in tool_msgs[0]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Preflight Gate (Rejection Cases)
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightGate:
    """Verify preflight rejects invalid/empty/duplicate/unroutable messages."""

    @pytest.mark.asyncio
    async def test_preflight_rejects_empty_message(self, tmp_path: Path) -> None:
        """Empty text should fail preflight."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = Config(llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"))

        with patch("src.llm.AsyncOpenAI"):
            bot, db, *_ = _make_bot(workspace, config, None)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-empty",
                chat_id="chat-1",
                sender_id="user-1",
                sender_name="Alice",
                text="",
                timestamp=1000.0,
            )
            result = await bot.preflight_check(msg)

        await db.close()
        assert not result.passed
        assert result.reason == "empty"

    @pytest.mark.asyncio
    async def test_preflight_rejects_duplicate(self, tmp_path: Path) -> None:
        """Duplicate message_id should fail preflight after first processing."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = Config(llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"))

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(return_value=_make_text_response("OK"))

            bot, db, *_ = _make_bot(workspace, config, None)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-dup-001",
                chat_id="chat-dup",
                sender_id="user-1",
                sender_name="Alice",
                text="First",
                timestamp=1000.0,
            )

            # First message processes successfully
            response = await bot.handle_message(msg)
            assert response is not None

            # Preflight should now reject the duplicate
            preflight = await bot.preflight_check(msg)
            assert not preflight.passed
            assert preflight.reason == "duplicate"

        await db.close()

    @pytest.mark.asyncio
    async def test_preflight_rejects_no_routing_match(self, tmp_path: Path) -> None:
        """Message that matches no routing rule should fail preflight."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        config = Config(llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"))

        with patch("src.llm.AsyncOpenAI"):
            # Create engine with NO rules
            routing = RoutingEngine(workspace)
            routing._rules = []

            from src.llm import LLMClient

            db = Database(str(workspace / ".data"))
            await db.connect()
            memory = Memory(str(workspace))
            skills = SkillRegistry()
            llm = LLMClient(config.llm)

            bot = Bot(
                config=config,
                db=db,
                llm=llm,
                memory=memory,
                skills=skills,
                routing=routing,
            )

            msg = IncomingMessage(
                message_id="msg-no-route",
                chat_id="chat-1",
                sender_id="user-1",
                sender_name="Alice",
                text="Hello",
                timestamp=1000.0,
            )
            preflight = await bot.preflight_check(msg)

        await db.close()
        assert not preflight.passed
        assert preflight.reason == "no_routing_rule"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Message Queue Integration
# ─────────────────────────────────────────────────────────────────────────────


class TestMessageQueueIntegration:
    """Verify the message queue tracks messages through the pipeline."""

    @pytest.mark.asyncio
    async def test_queue_enqueue_and_complete(self, tmp_path: Path) -> None:
        """
        When a message queue is configured, messages are enqueued
        before processing and completed after success.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"))

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Done!")
            )

            bot, db, memory, skills, queue = _make_bot(workspace, config, None, with_queue=True)
            await db.connect()
            assert queue is not None
            await queue.connect()

            msg = IncomingMessage(
                message_id="msg-queue-001",
                chat_id="chat-queue",
                sender_id="user-1",
                sender_name="Alice",
                text="Hello queue",
                timestamp=1000.0,
            )

            response = await bot.handle_message(msg)

        assert response is not None
        assert "Done" in response

        # After processing, the queue should have no pending messages
        pending = await queue.get_pending_count()
        assert pending == 0, "Message should be completed (not pending)"

        await queue.close()
        await db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Test: Multi-Step Tool Chain
# ─────────────────────────────────────────────────────────────────────────────


class TestMultiStepToolChain:
    """Pipeline with multiple sequential tool calls in one ReAct loop."""

    @pytest.mark.asyncio
    async def test_two_tool_calls_then_response(self, tmp_path: Path) -> None:
        """
        LLM calls two tools (echo + append_note) before producing
        a final text response, exercising the full loop.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test",
                model="gpt-4o-mini",
                max_tool_iterations=5,
            )
        )

        skills = SkillRegistry()
        skills._skills["echo"] = _EchoSkill()
        skills._skills["append_note"] = _AppendSkill()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            call_count = [0]

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                call_count[0] += 1
                if call_count[0] == 1:
                    return _make_tool_call_response(
                        "echo", {"text": "step one"}, tool_call_id="call_1"
                    )
                if call_count[0] == 2:
                    return _make_tool_call_response(
                        "append_note", {"content": "step two"}, tool_call_id="call_2"
                    )
                return _make_text_response("Completed both steps!")

            mock_client.chat.completions.create = _create

            bot, db, memory, _, queue = _make_bot(workspace, config, None, skills=skills)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-chain-001",
                chat_id="chat-chain",
                sender_id="user-1",
                sender_name="Alice",
                text="Run two steps",
                timestamp=1000.0,
            )

            response = await bot.handle_message(msg)

        await db.close()

        assert response is not None
        assert "Completed" in response
        assert call_count[0] == 3, "LLM should be called 3 times (2 tools + 1 final)"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Routing Rule Selection
# ─────────────────────────────────────────────────────────────────────────────


class TestRoutingRuleSelection:
    """Verify the pipeline selects the correct routing rule for messages."""

    @pytest.mark.asyncio
    async def test_specific_rule_overrides_catch_all(self, tmp_path: Path) -> None:
        """
        When multiple rules exist, the highest-priority (lowest number) one wins.
        The pipeline should route to the specific rule's instruction.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini"),
        )

        # Create instruction files so InstructionLoader can find them
        instructions_dir = workspace / "instructions"
        instructions_dir.mkdir(parents=True, exist_ok=True)
        (instructions_dir / "vip.agent.md").write_text(
            "You are a VIP assistant. Be extra helpful.", encoding="utf-8"
        )
        (instructions_dir / "chat.agent.md").write_text(
            "You are a helpful assistant.", encoding="utf-8"
        )

        # Build routing engine with a specific rule + catch-all
        routing = RoutingEngine(workspace)
        routing._rules = [
            RoutingRule(
                id="vip-rule",
                priority=10,
                sender="vip-user",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="vip.agent.md",
                enabled=True,
            ),
            RoutingRule(
                id="catch-all",
                priority=100,
                sender="*",
                recipient="*",
                channel="*",
                content_regex="*",
                instruction="chat.agent.md",
                enabled=True,
            ),
        ]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            captured_rule_ids: list[str] = []

            # Patch _build_turn_context to capture which rule matched
            original_build_turn = Bot._build_turn_context

            async def _capturing_build_turn(
                bot_self: Bot, msg: IncomingMessage, channel: Any = None
            ) -> Any:
                result = await original_build_turn(bot_self, msg, channel)
                if result:
                    captured_rule_ids.append(result.rule_id)
                return result

            with patch.object(Bot, "_build_turn_context", _capturing_build_turn):
                mock_client.chat.completions.create = AsyncMock(
                    return_value=_make_text_response("VIP response")
                )

                from src.llm import LLMClient

                db = Database(str(workspace / ".data"))
                await db.connect()
                memory = Memory(str(workspace))
                skills = SkillRegistry()
                llm = LLMClient(config.llm)

                bot = Bot(
                    config=config,
                    db=db,
                    llm=llm,
                    memory=memory,
                    skills=skills,
                    routing=routing,
                    instructions_dir=str(instructions_dir),
                )

                # Message from VIP user
                vip_msg = IncomingMessage(
                    message_id="msg-vip-001",
                    chat_id="chat-vip",
                    sender_id="vip-user",
                    sender_name="VIP",
                    text="Hello",
                    timestamp=1000.0,
                )

                response = await bot.handle_message(vip_msg)

        await db.close()

        assert response is not None
        assert captured_rule_ids == ["vip-rule"], (
            f"VIP message should match vip-rule, got: {captured_rule_ids}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test: Preflight + handle_message Dedup Consistency
# ─────────────────────────────────────────────────────────────────────────────


class TestPreflightHandleDedupConsistency:
    """Verify preflight_check and handle_message agree on dedup decisions.

    Guards against a race condition where preflight_check passes for a message
    but handle_message rejects it as duplicate (because another coroutine
    processed it between the two calls), or where a duplicate-according-to-
    preflight still gets processed by handle_message.
    """

    @pytest.mark.asyncio
    async def test_duplicate_rejected_by_both_preflight_and_handle(self, tmp_path: Path) -> None:
        """
        After a message is processed, BOTH preflight_check and
        handle_message must reject the same message_id as a duplicate.
        handle_message must return None (no LLM call).
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        llm_call_count = [0]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                llm_call_count[0] += 1
                return _make_text_response("First response")

            mock_client.chat.completions.create = _create

            bot, db, *_ = _make_bot(workspace, config, None)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-dedup-consistency-001",
                chat_id="chat-dedup",
                sender_id="user-1",
                sender_name="Alice",
                text="Original message",
                timestamp=1000.0,
            )

            # First: process normally — preflight passes then handle_message succeeds
            preflight_first = await bot.preflight_check(msg)
            assert preflight_first.passed, "First preflight should pass"

            response_first = await bot.handle_message(msg)
            assert response_first is not None, "First handle_message should return a response"
            assert llm_call_count[0] == 1, "LLM should be called exactly once"

            # Now both preflight and handle_message must reject as duplicate
            preflight_dup = await bot.preflight_check(msg)
            assert not preflight_dup.passed, "Second preflight should reject"
            assert preflight_dup.reason == "duplicate"

            response_dup = await bot.handle_message(msg)
            assert response_dup is None, (
                "handle_message must return None for duplicate, ensuring no second LLM call"
            )
            assert llm_call_count[0] == 1, "LLM must NOT be called again for a duplicate message"

        await db.close()

    @pytest.mark.asyncio
    async def test_concurrent_preflight_pass_then_handle_rejects(self, tmp_path: Path) -> None:
        """
        Race condition: preflight_check passes, but between preflight and
        handle_message, another coroutine processes the same message_id.
        handle_message must still reject the duplicate and not call the LLM.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        llm_call_count = [0]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                llm_call_count[0] += 1
                return _make_text_response("Processed response")

            mock_client.chat.completions.create = _create

            bot, db, *_ = _make_bot(workspace, config, None)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-race-001",
                chat_id="chat-race",
                sender_id="user-1",
                sender_name="Alice",
                text="Race condition message",
                timestamp=1000.0,
            )

            # Step 1: preflight passes (message not yet in DB)
            preflight = await bot.preflight_check(msg)
            assert preflight.passed, "Preflight should pass before any processing has occurred"

            # Step 2: simulate another coroutine processing the message
            # between preflight and handle_message
            response_sneaky = await bot.handle_message(msg)
            assert response_sneaky is not None, "First handle_message should succeed"
            assert llm_call_count[0] == 1

            # Step 3: the original caller now calls handle_message with the
            # same message_id — must be rejected as duplicate
            response_dup = await bot.handle_message(msg)
            assert response_dup is None, (
                "handle_message must return None when the message was already "
                "processed between preflight and handle_message"
            )
            assert llm_call_count[0] == 1, "No second LLM call should happen for the duplicate"

        await db.close()

    @pytest.mark.asyncio
    async def test_double_handle_message_no_duplicate_llm_calls(self, tmp_path: Path) -> None:
        """
        Calling handle_message() twice with the same message_id must only
        produce one LLM call. The second call returns None.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        llm_call_count = [0]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                llm_call_count[0] += 1
                return _make_text_response("Response")

            mock_client.chat.completions.create = _create

            bot, db, *_ = _make_bot(workspace, config, None)
            await db.connect()

            msg = IncomingMessage(
                message_id="msg-double-handle-001",
                chat_id="chat-double",
                sender_id="user-1",
                sender_name="Bob",
                text="Hello",
                timestamp=1000.0,
            )

            # First handle_message: succeeds
            response1 = await bot.handle_message(msg)
            assert response1 is not None
            assert llm_call_count[0] == 1

            # Second handle_message: rejected as duplicate, no LLM call
            response2 = await bot.handle_message(msg)
            assert response2 is None, "Second handle_message must return None"
            assert llm_call_count[0] == 1, "No duplicate LLM call"

            # Confirm preflight also now rejects
            preflight = await bot.preflight_check(msg)
            assert not preflight.passed
            assert preflight.reason == "duplicate"

        await db.close()

    @pytest.mark.asyncio
    async def test_different_message_ids_both_processed(self, tmp_path: Path) -> None:
        """
        Two different message_ids from the same chat should both be
        processed independently — dedup must not over-reject.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        llm_call_count = [0]

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                llm_call_count[0] += 1
                return _make_text_response(f"Response {llm_call_count[0]}")

            mock_client.chat.completions.create = _create

            bot, db, *_ = _make_bot(workspace, config, None)
            await db.connect()

            msg_a = IncomingMessage(
                message_id="msg-indep-a",
                chat_id="chat-indep",
                sender_id="user-1",
                sender_name="Alice",
                text="Message A",
                timestamp=1000.0,
            )

            msg_b = IncomingMessage(
                message_id="msg-indep-b",
                chat_id="chat-indep",
                sender_id="user-1",
                sender_name="Alice",
                text="Message B",
                timestamp=1001.0,
            )

            # Both should pass preflight
            preflight_a = await bot.preflight_check(msg_a)
            preflight_b = await bot.preflight_check(msg_b)
            assert preflight_a.passed
            assert preflight_b.passed

            # Both should be processed
            response_a = await bot.handle_message(msg_a)
            response_b = await bot.handle_message(msg_b)
            assert response_a is not None
            assert response_b is not None
            assert llm_call_count[0] == 2, "Two distinct messages = two LLM calls"

        await db.close()
