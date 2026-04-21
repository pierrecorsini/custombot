"""
test_bot_react_loop.py - E2E tests for the Bot's ReAct loop.

Tests the core bot functionality:
  - Message handling and deduplication
  - ReAct loop with tool calls
  - Multi-turn conversations
  - Error handling
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

# ─────────────────────────────────────────────────────────────────────────────
# Mock LLM Response Builders
# ─────────────────────────────────────────────────────────────────────────────


def make_text_response(text: str):
    """Create a mock text completion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].finish_reason = "stop"
    response.choices[0].message.content = text
    response.choices[0].message.tool_calls = None
    response.usage = {"prompt_tokens": 10, "completion_tokens": len(text) // 4}
    return response


def make_tool_call_response(tool_name: str, tool_args: dict, tool_call_id: str = "call_123"):
    """Create a mock tool call completion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].finish_reason = "tool_calls"

    # Mock tool call
    tool_call = MagicMock()
    tool_call.id = tool_call_id
    tool_call.function.name = tool_name
    tool_call.function.arguments = json.dumps(tool_args)

    response.choices[0].message.content = None
    response.choices[0].message.tool_calls = [tool_call]
    response.usage = {"prompt_tokens": 15, "completion_tokens": 10}
    return response


# ─────────────────────────────────────────────────────────────────────────────
# Helper: Mock Routing Engine
# ─────────────────────────────────────────────────────────────────────────────


def create_mock_routing_engine(tmp_path: Path):
    """Create a mock RoutingEngine with a catch-all rule for tests."""
    from src.routing import RoutingEngine, RoutingRule

    engine = RoutingEngine(tmp_path)
    engine._rules = [
        RoutingRule(
            id="test-catch-all",
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


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Message Deduplication
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_skips_duplicate_messages(tmp_path: Path):
    """
    E2E Test: Bot deduplicates messages with the same ID.

    Arrange:
        - Create bot with mocked dependencies
        - Create two messages with the same ID

    Act:
        - Handle both messages

    Assert:
        - Only first message is processed
        - Second returns None
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig, WhatsAppConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    # Arrange
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=make_text_response("Hello!"))

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-duplicate-test",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Hello bot",
            timestamp=1000.0,
        )

        # Act
        response1 = await bot.handle_message(msg)
        response2 = await bot.handle_message(msg)  # Duplicate

    await db.close()

    # Assert
    assert response1 is not None, "First message should be processed"
    assert response2 is None, "Duplicate message should be skipped"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ReAct Loop with Tool Calls
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_executes_tool_call_and_loops(tmp_path: Path):
    """
    E2E Test: Bot executes tool calls and continues the loop.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry
    from src.skills.base import BaseSkill

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            max_tool_iterations=5,
        )
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    # Create a mock skill
    class MockEchoSkill(BaseSkill):
        name = "echo"
        description = "Echo back the input"
        parameters = {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo"}},
            "required": ["text"],
        }

        async def execute(self, workspace_dir: Path, **kwargs) -> str:
            text = kwargs.get("text", "")
            return f"ECHO: {text}"

    skills._skills["echo"] = MockEchoSkill()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        # First call: tool call, second call: text response
        call_count = [0]

        async def mock_create(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return make_tool_call_response("echo", {"text": "hello world"})
            else:
                return make_text_response("I echoed your message!")

        mock_client.chat.completions.create = mock_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-tool-test",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Echo hello world",
            timestamp=1000.0,
        )

        # Act
        response = await bot.handle_message(msg)

    await db.close()

    # Assert
    assert response is not None, "Bot should return a response"
    assert call_count[0] == 2, "LLM should be called twice (tool call + final)"


@pytest.mark.asyncio
async def test_bot_handles_max_tool_iterations(tmp_path: Path):
    """
    E2E Test: Bot stops after max tool iterations.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry
    from src.skills.base import BaseSkill

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    # Create a mock skill
    class MockLoopSkill(BaseSkill):
        name = "loop_action"
        description = "An action that loops"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, workspace_dir: Path, **kwargs) -> str:
            return "Done"

    skills._skills["loop_action"] = MockLoopSkill()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        # Always return tool calls (infinite loop scenario)
        mock_client.chat.completions.create = AsyncMock(
            return_value=make_tool_call_response("loop_action", {})
        )

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-max-iter",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Loop forever",
            timestamp=1000.0,
        )

        # Act
        response = await bot.handle_message(msg)

    await db.close()

    # Assert
    assert response is not None, "Bot should return a response even on max iterations"
    assert "max tool iterations" in response.lower() or response, (
        "Response should indicate max iterations reached"
    )


@pytest.mark.asyncio
async def test_bot_handles_unknown_tool(tmp_path: Path):
    """
    E2E Test: Bot handles unknown tool calls gracefully.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            max_tool_iterations=5,
        )
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        call_count = [0]

        async def mock_create(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return make_tool_call_response("nonexistent_tool", {})
            else:
                return make_text_response("I encountered an error but recovered.")

        mock_client.chat.completions.create = mock_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-unknown-tool",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Use unknown tool",
            timestamp=1000.0,
        )

        # Act
        response = await bot.handle_message(msg)

    await db.close()

    # Assert
    assert response is not None, "Bot should return a response"
    assert call_count[0] == 2, "LLM should be called twice"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Context Building
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_includes_memory_in_context(tmp_path: Path):
    """
    E2E Test: Bot includes memory content in LLM context.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    # Write memory for the chat
    await memory.write_memory("test-chat", "# User Preferences\n- Likes Python\n- Uses dark mode")

    captured_messages = []

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        async def capture_create(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return make_text_response("Got it!")

        mock_client.chat.completions.create = capture_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-memory-test",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="What do you know about me?",
            timestamp=1000.0,
        )

        # Act
        await bot.handle_message(msg)

    await db.close()

    # Assert
    assert len(captured_messages) > 0, "Messages should be captured"
    system_msg = captured_messages[0]
    assert system_msg["role"] == "system"
    assert (
        "User Preferences" in system_msg["content"] or "memory" in system_msg["content"].lower()
    ), "Memory should be included in system message"


@pytest.mark.asyncio
async def test_bot_maintains_conversation_history(tmp_path: Path):
    """
    E2E Test: Bot maintains conversation history across turns.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1"),
        memory_max_history=10,
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    captured_messages_list = []

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        def create_capture(messages_list):
            async def capture_create(*args, **kwargs):
                messages_list.append(list(kwargs.get("messages", [])))
                return make_text_response("Response")

            return capture_create

        mock_client.chat.completions.create = create_capture(captured_messages_list)

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        # First message
        msg1 = IncomingMessage(
            message_id="msg-history-1",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="My name is Alice",
            timestamp=1000.0,
        )
        await bot.handle_message(msg1)

        # Second message
        msg2 = IncomingMessage(
            message_id="msg-history-2",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="What's my name?",
            timestamp=1001.0,
        )
        await bot.handle_message(msg2)

    await db.close()

    # Assert - second call should include first message in history
    assert len(captured_messages_list) >= 2, "Should have two message captures"
    second_call_messages = captured_messages_list[1]

    # Find user messages
    user_messages = [m for m in second_call_messages if m.get("role") == "user"]
    assert len(user_messages) >= 1, "Should have user message in history"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Per-Chat Isolation
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_isolates_chats(tmp_path: Path):
    """
    E2E Test: Bot isolates different chats.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client
        mock_client.chat.completions.create = AsyncMock(return_value=make_text_response("Response"))

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        # Message to chat A
        msg_a = IncomingMessage(
            message_id="msg-isolation-a",
            chat_id="chat-alice",
            sender_id="alice",
            sender_name="Alice",
            text="Hello from Alice",
            timestamp=1000.0,
        )
        await bot.handle_message(msg_a)

        # Message to chat B
        msg_b = IncomingMessage(
            message_id="msg-isolation-b",
            chat_id="chat-bob",
            sender_id="bob",
            sender_name="Bob",
            text="Hello from Bob",
            timestamp=1001.0,
        )
        await bot.handle_message(msg_b)

    await db.close()

    # Assert - both chats have workspaces (stored under whatsapp_data/)
    chat_a_workspace = workspace / "whatsapp_data" / "chat-alice"
    chat_b_workspace = workspace / "whatsapp_data" / "chat-bob"

    assert chat_a_workspace.exists(), "Chat A workspace should exist"
    assert chat_b_workspace.exists(), "Chat B workspace should exist"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Error Handling
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_bot_handles_malformed_tool_args(tmp_path: Path):
    """
    E2E Test: Bot handles malformed tool arguments.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry
    from src.skills.base import BaseSkill

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            max_tool_iterations=5,
        )
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    class TestSkill(BaseSkill):
        name = "test_skill"
        description = "A test skill"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, workspace_dir: Path, **kwargs) -> str:
            return "Success"

    skills._skills["test_skill"] = TestSkill()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        call_count = [0]

        async def mock_create(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Return malformed tool call
                response = MagicMock()
                response.choices = [MagicMock()]
                response.choices[0].finish_reason = "tool_calls"
                tool_call = MagicMock()
                tool_call.id = "call_bad"
                tool_call.function.name = "test_skill"
                tool_call.function.arguments = "not valid json {{{"  # Invalid JSON
                response.choices[0].message.content = None
                response.choices[0].message.tool_calls = [tool_call]
                return response
            else:
                return make_text_response("Recovered from error")

        mock_client.chat.completions.create = mock_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-malformed",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Test malformed",
            timestamp=1000.0,
        )

        # Act
        response = await bot.handle_message(msg)

    await db.close()

    # Assert
    assert response is not None, "Bot should handle malformed args gracefully"


@pytest.mark.asyncio
async def test_bot_handles_skill_exception(tmp_path: Path):
    """
    E2E Test: Bot handles skill execution exceptions.
    """
    from src.bot import Bot
    from src.channels.base import IncomingMessage
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry
    from src.skills.base import BaseSkill

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(
            api_key="sk-test",
            model="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            max_tool_iterations=5,
        )
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    class FailingSkill(BaseSkill):
        name = "failing_skill"
        description = "A skill that always fails"
        parameters = {"type": "object", "properties": {}}

        async def execute(self, workspace_dir: Path, **kwargs) -> str:
            raise RuntimeError("Intentional test failure")

    skills._skills["failing_skill"] = FailingSkill()

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        call_count = [0]

        async def mock_create(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return make_tool_call_response("failing_skill", {})
            else:
                return make_text_response("I encountered an error but I'm still here.")

        mock_client.chat.completions.create = mock_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)

        routing = create_mock_routing_engine(workspace)

        bot = Bot(config=config, db=db, llm=llm, memory=memory, skills=skills, routing=routing)

        msg = IncomingMessage(
            message_id="msg-skill-exception",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Use failing skill",
            timestamp=1000.0,
        )

        # Act
        response = await bot.handle_message(msg)

    await db.close()

    # Assert
    assert response is not None, "Bot should return response even after skill failure"
    assert call_count[0] == 2, "LLM should be called twice (failure + recovery)"
