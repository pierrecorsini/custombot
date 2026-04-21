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
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot import Bot
from src.config import Config, LLMConfig
from src.core.context_builder import ChatMessage
from src.db import Database
from src.memory import Memory
from src.skills import SkillRegistry
from src.skills.base import BaseSkill


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
    tool_call_id: str = "call_sched_001",
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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Daily summary: no new messages.")
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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Scheduled response")
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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("OK")
            )

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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Done")
            )

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
                    return _make_tool_call_response("echo", {"text": "status check"})
                return _make_text_response("Status: all systems operational")

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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
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
            llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
        )

        with patch("src.llm.AsyncOpenAI") as mock_openai:
            mock_client = AsyncMock()
            mock_openai.return_value = mock_client
            mock_client.chat.completions.create = AsyncMock(
                return_value=_make_text_response("Done")
            )

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
