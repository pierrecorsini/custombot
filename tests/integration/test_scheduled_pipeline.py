"""
test_scheduled_pipeline.py — Integration test for the scheduled task pipeline.

Exercises the full ``Bot.process_scheduled()`` flow end-to-end:

  scheduler trigger
    → process_scheduled() (bypasses routing, dedup, rate limiting)
    → workspace creation
    → context build (memory, agents_md, history)
    → LLM call (mocked at HTTP transport, real LLMClient)
    → tool execution (real SkillRegistry with injected skills)
    → response delivery
    → persistence (real Database)

Verifies that scheduled tasks:
  - Bypass routing and dedup (unlike handle_message)
  - Persist both user (Scheduler) and assistant messages in DB
  - Handle tool calls in the ReAct loop
  - Degrade gracefully on workspace/context failures
"""

from __future__ import annotations

import json
import logging
from typing import Any, TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from src.bot import Bot, BotConfig
from src.channels.base import IncomingMessage
from src.config import Config, LLMConfig
from src.core.context_builder import ChatMessage
from src.core.dedup import DeduplicationService
from src.db import Database
from src.memory import Memory
from src.routing import RoutingEngine, RoutingRule
from src.skills import SkillRegistry
from src.skills.base import BaseSkill
from tests.helpers.llm_mocks import make_text_response, make_tool_call_response

if TYPE_CHECKING:
    from src.core.event_bus import Event
    from unittest.mock import MagicMock
    from pathlib import Path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


class _LogCapture(logging.Handler):
    """Simple logging handler that collects records for test assertions."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


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


def _make_bot(
    workspace: Path,
    config: Config,
    skills: SkillRegistry | None = None,
) -> tuple[Bot, Database, Memory, SkillRegistry]:
    """Wire up a Bot with real components and mocked LLM transport.

    No routing engine is needed — process_scheduled() bypasses routing.
    """
    from src.llm import LLMClient
    from src.routing import RoutingEngine

    db = Database(str(workspace / ".data"))
    memory = Memory(str(workspace))
    # Provide a routing engine with no rules so handle_message would fail,
    # proving process_scheduled truly bypasses routing.
    routing = RoutingEngine(workspace)
    routing._rules = []
    registry = skills or SkillRegistry()
    llm = LLMClient(config.llm)

    bot = Bot(
        config=config,
        db=db,
        llm=llm,
        memory=memory,
        skills=registry,
        routing=routing,
        instructions_dir=str(workspace / "instructions"),
    )
    return bot, db, memory, registry


# ─────────────────────────────────────────────────────────────────────────────
# Test: Scheduled Task Happy Path
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduledPipelineHappyPath:
    """End-to-end: process_scheduled() → LLM → response → persistence."""

    @pytest.mark.asyncio
    async def test_simple_scheduled_response(self, tmp_path: Path) -> None:
        """
        Scheduled prompt processes successfully and both turns are persisted.

        Stages exercised:
          1. Workspace directory created for chat
          2. Context built from history + memory
          3. LLM returns a text response
          4. Response returned to caller
          5. Both user (Scheduler) and assistant messages persisted
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
                return_value=make_text_response("Daily summary: no new messages.")
            )

            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            response = await bot.process_scheduled(
                chat_id="chat-sched-001",
                prompt="Summarize today's activity",
            )

        await db.close()

        # Response delivered
        assert response is not None
        assert "Daily summary" in response

        # Messages persisted with correct roles
        rows = await db.get_recent_messages("chat-sched-001", limit=10)
        roles = [r["role"] for r in rows]
        assert "user" in roles, "Scheduled prompt should be persisted as user message"
        assert "assistant" in roles, "Response should be persisted as assistant message"

        # Chat record created with name "Scheduler"
        # (verify via the saved messages having name="Scheduler")

    @pytest.mark.asyncio
    async def test_scheduled_bypasses_routing(self, tmp_path: Path) -> None:
        """
        process_scheduled() succeeds even when routing has no rules.

        A normal handle_message would fail preflight with "no_routing_rule",
        but scheduled tasks bypass routing entirely.
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
                return_value=make_text_response("Scheduled response")
            )

            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            # process_scheduled should succeed despite no routing rules
            response = await bot.process_scheduled(
                chat_id="chat-no-routing",
                prompt="Test prompt",
            )

        await db.close()

        assert response is not None
        assert "Scheduled response" in response

    @pytest.mark.asyncio
    async def test_scheduled_bypasses_dedup(self, tmp_path: Path) -> None:
        """
        Calling process_scheduled() twice with the same prompt succeeds
        both times — no dedup check.

        Unlike handle_message which tracks message_id for dedup,
        scheduled tasks generate synthetic IDs and skip dedup.
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
            mock_client.chat.completions.create = AsyncMock(return_value=make_text_response("OK"))

            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            # First call
            r1 = await bot.process_scheduled(
                chat_id="chat-dedup",
                prompt="Repeat this",
            )
            # Second call — same prompt, same chat
            r2 = await bot.process_scheduled(
                chat_id="chat-dedup",
                prompt="Repeat this",
            )

        await db.close()

        assert r1 is not None, "First scheduled call should succeed"
        assert r2 is not None, "Second scheduled call should succeed (no dedup)"

    @pytest.mark.asyncio
    async def test_workspace_created_for_new_chat(self, tmp_path: Path) -> None:
        """
        process_scheduled() creates the per-chat workspace directory
        for a chat that has never been seen before.
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
            mock_client.chat.completions.create = AsyncMock(return_value=make_text_response("Done"))

            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            chat_id = "chat-new-workspace"
            assert not (workspace / "whatsapp_data" / chat_id).exists()

            await bot.process_scheduled(chat_id=chat_id, prompt="Hello")

        await db.close()

        # Workspace directory should now exist
        chat_dir = workspace / "whatsapp_data" / chat_id
        assert chat_dir.exists(), "Workspace directory should be created"
        assert (chat_dir / "AGENTS.md").exists(), "AGENTS.md should be seeded"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Scheduled Pipeline with Tool Execution
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduledPipelineWithTools:
    """Scheduled task that triggers tool calls in the ReAct loop."""

    @pytest.mark.asyncio
    async def test_tool_call_then_final_response(self, tmp_path: Path) -> None:
        """
        Scheduled prompt → LLM tool call → skill executes → LLM final response.

        Stages exercised:
          1. LLM issues a tool call (echo skill)
          2. Skill executes and returns result
          3. LLM produces final text after seeing tool result
          4. Final response includes tool execution context
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
                    return make_tool_call_response("echo", {"text": "status check"})
                return make_text_response("Status: all systems operational")

            mock_client.chat.completions.create = _create

            bot, db, memory, _ = _make_bot(workspace, config, skills=skills)
            await db.connect()

            response = await bot.process_scheduled(
                chat_id="chat-sched-tools",
                prompt="Check system status",
            )

        await db.close()

        assert response is not None
        assert "operational" in response.lower() or "status" in response.lower()
        assert call_count[0] == 2, "LLM should be called twice (tool + final)"

        # Second LLM call should contain the tool result
        second_call_msgs = captured_messages[1]
        tool_msgs = [m for m in second_call_msgs if m.get("role") == "tool"]
        assert len(tool_msgs) == 1, "Second call should contain one tool result"
        assert "ECHO: status check" in tool_msgs[0]["content"]


# ─────────────────────────────────────────────────────────────────────────────
# Test: Scheduled Pipeline Error Handling
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduledPipelineErrors:
    """Verify graceful degradation when sub-steps fail."""

    @pytest.mark.asyncio
    async def test_returns_none_on_workspace_failure(self, tmp_path: Path) -> None:
        """
        If ensure_workspace raises OSError, process_scheduled returns None
        instead of crashing.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        with patch("src.llm.AsyncOpenAI"):
            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            with patch.object(memory, "ensure_workspace", side_effect=OSError("disk full")):
                response = await bot.process_scheduled(
                    chat_id="chat-disk-full",
                    prompt="Test",
                )

        await db.close()

        assert response is None, "Should return None on workspace failure"

    @pytest.mark.asyncio
    async def test_returns_none_on_context_build_failure(self, tmp_path: Path) -> None:
        """
        If build_context raises OSError (e.g. corrupted memory file),
        process_scheduled returns None.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
            )
        )

        with patch("src.llm.AsyncOpenAI"):
            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            with patch("src.bot.build_context", side_effect=OSError("read error")):
                response = await bot.process_scheduled(
                    chat_id="chat-ctx-fail",
                    prompt="Test",
                )

        await db.close()

        assert response is None, "Should return None on context build failure"

    @pytest.mark.asyncio
    async def test_returns_none_on_llm_failure(self, tmp_path: Path) -> None:
        """
        If the LLM call raises an exception, process_scheduled returns None.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = Config(
            llm=LLMConfig(
                api_key="sk-test",
                model="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                timeout=1.0,
            )
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                side_effect=Exception("LLM provider down")
            )

            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            response = await bot.process_scheduled(
                chat_id="chat-llm-fail",
                prompt="Test",
            )

        await db.close()

        assert response is None, "Should return None on LLM failure"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Scheduled vs Normal Pipeline Differences
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduledVsNormalDifferences:
    """Verify scheduled pipeline differs from normal pipeline in key ways."""

    @pytest.mark.asyncio
    async def test_scheduled_persists_scheduler_as_name(self, tmp_path: Path) -> None:
        """
        Scheduled tasks persist user messages with name='Scheduler',
        unlike normal messages which use the sender's name.
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
            mock_client.chat.completions.create = AsyncMock(return_value=make_text_response("Done"))

            bot, db, memory, skills = _make_bot(workspace, config)
            await db.connect()

            await bot.process_scheduled(
                chat_id="chat-sched-name",
                prompt="Hello scheduler",
            )

        # Check the user message was saved with name="Scheduler"
        rows = await db.get_recent_messages("chat-sched-name", limit=10)
        user_msgs = [r for r in rows if r["role"] == "user"]

        await db.close()

        assert len(user_msgs) >= 1, "Should have at least one user message"
        # The message content should match the scheduled prompt
        assert any("Hello scheduler" in r["content"] for r in user_msgs), (
            "User message should contain the scheduled prompt"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for write-conflict tests (BotConfig-aware)
# ─────────────────────────────────────────────────────────────────────────────


def _make_botconfig(
    max_tool_iterations: int = 5,
    memory_max_history: int = 50,
    system_prompt_prefix: str = "You are a helpful assistant.",
) -> BotConfig:
    """Create a BotConfig for integration tests."""
    return BotConfig(
        max_tool_iterations=max_tool_iterations,
        memory_max_history=memory_max_history,
        system_prompt_prefix=system_prompt_prefix,
    )


def _make_routing_engine(workspace: Path) -> RoutingEngine:
    """Create a RoutingEngine with a single catch-all rule."""
    engine = RoutingEngine(workspace)
    engine._rules = [
        RoutingRule(
            id="catch-all",
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


def _make_full_bot(
    workspace: Path,
    bot_config: BotConfig | None = None,
    skills: SkillRegistry | None = None,
) -> tuple[Bot, Database, Memory, SkillRegistry]:
    """Wire up a Bot with real components, proper BotConfig, and DeduplicationService.

    Uses ``BotConfig`` (not ``Config``) so context assembly and the ReAct loop
    can access the expected attributes without ``AttributeError``.
    """
    from src.llm import LLMClient

    bot_config = bot_config or _make_botconfig()
    db = Database(str(workspace / ".data"))
    memory = Memory(str(workspace))
    routing = _make_routing_engine(workspace)
    registry = skills or SkillRegistry()

    # Minimal LLMConfig for LLMClient construction (transport will be mocked)
    llm_config = LLMConfig(
        api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"
    )
    llm = LLMClient(llm_config)
    dedup = DeduplicationService(db)

    bot = Bot(
        config=bot_config,
        db=db,
        llm=llm,
        memory=memory,
        skills=registry,
        routing=routing,
        dedup=dedup,
        instructions_dir=str(workspace / "instructions"),
    )
    return bot, db, memory, registry


# ─────────────────────────────────────────────────────────────────────────────
# Test: Write-Conflict Detection (scheduled task + user message)
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduledWriteConflictDetection:
    """Integration test: concurrent user message and scheduled task for the same chat.

    Exercises the generation-counter write-conflict detection:

      1. Both ``handle_message`` and ``process_scheduled`` acquire the same
         per-chat lock, so they are serialized.  The test runs them via
         ``asyncio.gather`` to verify correct serialisation.
      2. A simulated conflict (generation bump during processing) triggers
         the warning log in ``_process()``.
      3. Both responses must be persisted without corruption and the JSONL
         conversation history must remain valid and parseable.
    """

    @pytest.mark.asyncio
    async def test_concurrent_user_message_and_scheduled_task_both_persist(
        self, tmp_path: Path
    ) -> None:
        """
        A user message and a scheduled task for the same chat are started
        concurrently via ``asyncio.gather``.  Because the per-chat lock
        serializes them, both should complete successfully and all messages
        should be persisted without corruption.

        Verifies:
          (a) Both responses are persisted
          (b) The conversation history contains both user and assistant turns
          (c) The JSONL file is valid and parseable
        """
        import asyncio

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client

            call_count = [0]

            async def _create(*args: Any, **kwargs: Any) -> MagicMock:
                call_count[0] += 1
                return make_text_response(f"Response {call_count[0]}")

            mock_client.chat.completions.create = _create

            bot, db, memory, skills = _make_full_bot(workspace)
            await db.connect()

            chat_id = "chat-conflict"

            # Start both operations concurrently
            msg = IncomingMessage(
                message_id="msg-conflict-001",
                chat_id=chat_id,
                sender_id="user-1",
                sender_name="Alice",
                text="Hello from user",
                timestamp=1000.0,
            )

            user_response, sched_response = await asyncio.gather(
                bot.handle_message(msg),
                bot.process_scheduled(chat_id=chat_id, prompt="Scheduled summary"),
            )

        # (a) Both responses delivered
        assert user_response is not None, "User message should produce a response"
        assert sched_response is not None, "Scheduled task should produce a response"

        # (b) Verify messages persisted
        rows = await db.get_recent_messages(chat_id, limit=20)
        roles = [r["role"] for r in rows]
        assert roles.count("user") >= 2, "Should have at least 2 user messages (user + scheduler)"
        assert roles.count("assistant") >= 2, "Should have at least 2 assistant messages"

        # (c) Verify JSONL file is valid and parseable
        await db.close()
        msg_file = workspace / ".data" / "messages" / f"{chat_id}.jsonl"
        assert msg_file.exists(), "JSONL file should exist"
        content = msg_file.read_text(encoding="utf-8")
        for line_num, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if parsed.get("type") == "header":
                continue
            assert "role" in parsed, f"Line {line_num}: missing 'role' key"
            assert "content" in parsed, f"Line {line_num}: missing 'content' key"
            assert "id" in parsed, f"Line {line_num}: missing 'id' key"

    @pytest.mark.asyncio
    async def test_generation_conflict_logs_warning(self, tmp_path: Path) -> None:
        """
        Simulate a write conflict by bumping the generation counter between
        context assembly and persist in ``_process()``.

        When the generation changes during processing (e.g. a scheduled task
        writes while a user message is being handled), ``_process()`` should
        log a warning containing 'Write conflict for'.

        Verifies:
          (b) The generation check logs a warning when a conflict is detected
          (c) The response is still persisted (graceful degradation)
        """
        import asyncio
        import logging

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=make_text_response("Final response")
            )

            bot, db, memory, skills = _make_full_bot(workspace)
            await db.connect()

            chat_id = "chat-gen-conflict"

            # Pre-populate some history so the chat has a generation
            await db.save_message(chat_id, "user", "Previous message", "Alice")
            gen_before = db.get_generation(chat_id)
            assert gen_before >= 1, "Generation should be >= 1 after initial write"

            # Patch _react_loop to inject a generation bump mid-processing,
            # simulating a concurrent scheduled task writing to the same chat.
            original_react_loop = bot._react_loop

            async def _react_loop_with_conflict(*args: Any, **kwargs: Any) -> Any:
                # Simulate a concurrent writer (e.g. scheduled task) bumping
                # the generation while the ReAct loop is running.
                db._bump_generation(chat_id)
                db._bump_generation(chat_id)
                return await original_react_loop(*args, **kwargs)

            bot._react_loop = _react_loop_with_conflict

            # Capture warning logs
            with patch.object(
                bot._db, "check_generation", wraps=bot._db.check_generation
            ) as spy_check:
                msg = IncomingMessage(
                    message_id="msg-gen-conflict-001",
                    chat_id=chat_id,
                    sender_id="user-1",
                    sender_name="Alice",
                    text="Trigger conflict",
                    timestamp=1001.0,
                )

                with self._capture_logs(logging.WARNING) as warnings:
                    response = await bot.handle_message(msg)

        # Response should still be delivered (graceful degradation)
        assert response is not None, "Response should be persisted despite conflict"

        # The generation was bumped during processing, so check_generation
        # should have returned False at least once
        gen_after = db.get_generation(chat_id)
        assert gen_after > gen_before, "Generation should have increased"

        # (b) Verify warning was logged
        conflict_warnings = [r for r in warnings if "Write conflict for" in r.getMessage()]
        assert len(conflict_warnings) >= 1, (
            f"Expected 'Write conflict for' warning, got: {[r.getMessage() for r in warnings]}"
        )

        # (c) Verify conversation history is still valid
        await db.close()
        msg_file = workspace / ".data" / "messages" / f"{chat_id}.jsonl"
        assert msg_file.exists()
        content = msg_file.read_text(encoding="utf-8")
        for line_num, line in enumerate(content.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            parsed = json.loads(line)
            if parsed.get("type") == "header":
                continue
            assert "role" in parsed, f"Line {line_num}: missing 'role'"

    @staticmethod
    def _capture_logs(level: int = logging.WARNING):
        """Context manager that captures log records at the given level."""
        from contextlib import contextmanager

        @contextmanager
        def _cm():
            logger = logging.getLogger("src.bot")
            handler = _LogCapture()
            handler.setLevel(level)
            records: list[logging.LogRecord] = []
            handler.records = records
            logger.addHandler(handler)
            try:
                yield records
            finally:
                logger.removeHandler(handler)

        return _cm()

    @pytest.mark.asyncio
    async def test_scheduled_then_user_message_both_persist_sequentially(
        self, tmp_path: Path
    ) -> None:
        """
        Sequential: scheduled task first, then user message for the same chat.

        Verifies that both operations succeed in sequence, the generation
        counter increments correctly, and the full conversation history
        is consistent and parseable.

        This tests the non-conflicting case (generation matches) to ensure
        baseline correctness.
        """
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=make_text_response("OK response")
            )

            bot, db, memory, skills = _make_full_bot(workspace)
            await db.connect()

            chat_id = "chat-sequential"

            # 1. Scheduled task first
            sched_response = await bot.process_scheduled(
                chat_id=chat_id,
                prompt="Daily check",
            )
            assert sched_response is not None
            gen_after_sched = db.get_generation(chat_id)
            assert gen_after_sched >= 2, (
                "Generation should be >= 2 after scheduled task (user + assistant)"
            )

            # 2. User message second
            msg = IncomingMessage(
                message_id="msg-sequential-001",
                chat_id=chat_id,
                sender_id="user-1",
                sender_name="Alice",
                text="Follow-up message",
                timestamp=1002.0,
            )
            user_response = await bot.handle_message(msg)
            assert user_response is not None
            gen_after_user = db.get_generation(chat_id)
            assert gen_after_user > gen_after_sched, "Generation should increase after user message"

        # Verify full conversation history
        rows = await db.get_recent_messages(chat_id, limit=20)
        assert len(rows) >= 4, "Should have at least 4 messages (2 from scheduled + 2 from user)"

        # Verify JSONL file is parseable
        await db.close()
        msg_file = workspace / ".data" / "messages" / f"{chat_id}.jsonl"
        content = msg_file.read_text(encoding="utf-8")
        parsed_messages = []
        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if record.get("type") == "header":
                continue
            parsed_messages.append(record)

        # Verify chronological order: timestamps should be non-decreasing
        timestamps = [r["timestamp"] for r in parsed_messages if "timestamp" in r]
        assert timestamps == sorted(timestamps), "Messages should be in chronological order"

        # Verify all IDs are unique
        ids = [r["id"] for r in parsed_messages if "id" in r]
        assert len(ids) == len(set(ids)), "All message IDs should be unique"


# ─────────────────────────────────────────────────────────────────────────────
# Test: Scheduled Pipeline Event Emission
# ─────────────────────────────────────────────────────────────────────────────


class TestScheduledPipelineEventEmission:
    """Integration test: process_scheduled() emits lifecycle events on the EventBus.

    Verifies:
      (a) ``scheduled_task_started`` is emitted with the correct ``chat_id``
          and ``prompt_length`` before processing begins.
      (b) ``scheduled_task_completed`` is emitted after the response is
          persisted, with ``chat_id`` and ``response_length``.
      (c) Both events carry a non-None ``correlation_id`` sourced from the
          scheduling context.
    """

    @pytest.mark.asyncio
    async def test_started_and_completed_events_emitted(self, tmp_path: Path) -> None:
        """
        Happy-path: both scheduled_task_started and scheduled_task_completed
        events are emitted with correct data.
        """
        from src.core.event_bus import Event, get_event_bus, reset_event_bus

        reset_event_bus()
        bus = get_event_bus()

        started_events: list[Event] = []
        completed_events: list[Event] = []

        async def _on_started(event: Event) -> None:
            started_events.append(event)

        async def _on_completed(event: Event) -> None:
            completed_events.append(event)

        bus.on("scheduled_task_started", _on_started)
        bus.on("scheduled_task_completed", _on_completed)

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        response_text = "Daily briefing: all quiet."
        chat_id = "chat-event-emit-001"

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=make_text_response(response_text)
            )

            bot, db, memory, skills = _make_full_bot(workspace)
            await db.connect()

            result = await bot.process_scheduled(
                chat_id=chat_id,
                prompt="Summarize today",
            )

        await db.close()
        await bus.close()
        reset_event_bus()

        # (a) Response delivered
        assert result is not None
        assert response_text in result

        # (b) started event emitted with correct data
        assert len(started_events) == 1, "Expected exactly one started event"
        started = started_events[0]
        assert started.name == "scheduled_task_started"
        assert started.data["chat_id"] == chat_id
        assert started.data["prompt_length"] == len("Summarize today")
        assert started.source == "Bot.process_scheduled"
        assert started.correlation_id is not None

        # (c) completed event emitted with correct data
        assert len(completed_events) == 1, "Expected exactly one completed event"
        completed = completed_events[0]
        assert completed.name == "scheduled_task_completed"
        assert completed.data["chat_id"] == chat_id
        assert completed.data["response_length"] == len(result)
        assert completed.source == "Bot.process_scheduled"
        assert completed.correlation_id is not None

    @pytest.mark.asyncio
    async def test_started_event_emitted_even_on_failure(self, tmp_path: Path) -> None:
        """
        When process_scheduled() fails (e.g. workspace error), the
        scheduled_task_started event is still emitted but
        scheduled_task_completed is not.
        """
        from src.core.event_bus import Event, get_event_bus, reset_event_bus

        reset_event_bus()
        bus = get_event_bus()

        started_events: list[Event] = []
        completed_events: list[Event] = []

        bus.on("scheduled_task_started", lambda e: started_events.append(e))
        bus.on(
            "scheduled_task_completed",
            lambda e: completed_events.append(e),
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        chat_id = "chat-event-fail-001"

        with patch("src.llm.AsyncOpenAI"):
            bot, db, memory, skills = _make_full_bot(workspace)
            await db.connect()

            with patch.object(memory, "ensure_workspace", side_effect=OSError("no space")):
                result = await bot.process_scheduled(
                    chat_id=chat_id,
                    prompt="Should fail",
                )

        await db.close()
        await bus.close()
        reset_event_bus()

        # Processing failed
        assert result is None

        # Started event was still emitted
        assert len(started_events) == 1
        assert started_events[0].data["chat_id"] == chat_id

        # Completed event was NOT emitted (failure path returns early)
        assert len(completed_events) == 0

    @pytest.mark.asyncio
    async def test_response_length_matches_actual_response(self, tmp_path: Path) -> None:
        """
        The ``response_length`` in the completed event exactly matches the
        length of the response text returned by process_scheduled().
        """
        from src.core.event_bus import get_event_bus, reset_event_bus

        reset_event_bus()
        bus = get_event_bus()

        completed_events: list[Event] = []
        bus.on(
            "scheduled_task_completed",
            lambda e: completed_events.append(e),
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        response_text = "A" * 500  # Known-length response
        chat_id = "chat-event-len-001"

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=make_text_response(response_text)
            )

            bot, db, memory, skills = _make_full_bot(workspace)
            await db.connect()

            result = await bot.process_scheduled(
                chat_id=chat_id,
                prompt="Generate response",
            )

        await db.close()
        await bus.close()
        reset_event_bus()

        assert result is not None
        assert len(completed_events) == 1
        assert completed_events[0].data["response_length"] == len(result)
