"""
test_whatsapp_channel_prompt.py - Tests for WhatsApp channel prompt injection.

Verifies that the WhatsApp formatting prompt reaches the LLM system message
when a WhatsAppChannel is passed to bot.handle_message().
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.bot._bot import BotConfig, BotDeps
from src.channels.base import IncomingMessage
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _make_text_response(text: str):
    """Create a mock text completion response."""
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].finish_reason = "stop"
    response.choices[0].message.content = text
    response.choices[0].message.tool_calls = None
    response.usage = {"prompt_tokens": 10, "completion_tokens": len(text) // 4}
    return response


def _create_mock_routing_engine(tmp_path: Path):
    """Create a mock RoutingEngine with a catch-all rule."""
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


@pytest.mark.asyncio
async def test_whatsapp_channel_prompt_injected_into_system_message(tmp_path: Path):
    """
    When channel=WhatsAppChannel is passed to handle_message,
    the WhatsApp formatting prompt must appear in the LLM system message.
    """
    from src.bot import Bot
    from src.channels.whatsapp import WhatsAppChannel
    from src.config import Config, LLMConfig, NeonizeConfig, WhatsAppConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    instructions_dir = workspace / "instructions"
    instructions_dir.mkdir()
    (instructions_dir / "chat.agent.md").write_text("You are a helpful assistant.")

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    captured_messages = []

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        async def capture_create(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return _make_text_response("Response")

        mock_client.chat.completions.create = capture_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)
        routing = _create_mock_routing_engine(workspace)

        bot_config = BotConfig(
            max_tool_iterations=5,
            memory_max_history=50,
            system_prompt_prefix="",
        )

        mock_dedup = AsyncMock()
        mock_dedup.is_inbound_duplicate = AsyncMock(return_value=False)

        bot = Bot(
            BotDeps(
                config=bot_config,
                db=db,
                llm=llm,
                memory=memory,
                skills=skills,
                routing=routing,
                instructions_dir=str(instructions_dir),
                dedup=mock_dedup,
                rate_limiter=MagicMock(),
                tool_executor=AsyncMock(),
                context_assembler=AsyncMock(),
            )
        )

        # Create a real WhatsAppChannel
        wa_config = WhatsAppConfig(
            neonize=NeonizeConfig(db_path=str(workspace / "test_session.db"))
        )
        channel = WhatsAppChannel(wa_config)

        msg = IncomingMessage(
            message_id="msg-wa-prompt-test",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Hello",
            timestamp=1000.0,
            acl_passed=True,
        )

        # Act — pass the channel
        await bot.handle_message(msg, channel=channel)

    await db.close()

    # Assert
    assert len(captured_messages) > 0, "LLM should have been called"
    system_msg = captured_messages[0]
    assert system_msg["role"] == "system"
    system_content = system_msg["content"]

    # The WhatsApp prompt must contain these key phrases
    assert "WhatsApp" in system_content, "System message must mention WhatsApp"
    assert "tables" in system_content.lower(), "Must address table formatting"
    assert "headers" in system_content.lower() or "NO #" in system_content, (
        "Must address header formatting"
    )


@pytest.mark.asyncio
async def test_no_channel_means_no_channel_prompt(tmp_path: Path):
    """
    When channel=None (no channel passed), the system message should NOT
    contain WhatsApp formatting instructions.
    """
    from src.bot import Bot
    from src.config import Config, LLMConfig
    from src.db import Database
    from src.memory import Memory
    from src.skills import SkillRegistry

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    instructions_dir = workspace / "instructions"
    instructions_dir.mkdir()
    (instructions_dir / "chat.agent.md").write_text("You are a helpful assistant.")

    db = Database(str(workspace / "test.db"))
    await db.connect()

    config = Config(
        llm=LLMConfig(api_key="sk-test", model="gpt-4o-mini", base_url="https://api.openai.com/v1")
    )
    memory = Memory(str(workspace))
    skills = SkillRegistry()

    captured_messages = []

    with patch("src.llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        async def capture_create(*args, **kwargs):
            captured_messages.extend(kwargs.get("messages", []))
            return _make_text_response("Response")

        mock_client.chat.completions.create = capture_create

        from src.llm import LLMClient

        llm = LLMClient(config.llm)
        routing = _create_mock_routing_engine(workspace)

        bot_config = BotConfig(
            max_tool_iterations=5,
            memory_max_history=50,
            system_prompt_prefix="",
        )

        mock_dedup = AsyncMock()
        mock_dedup.is_inbound_duplicate = AsyncMock(return_value=False)

        bot = Bot(
            BotDeps(
                config=bot_config,
                db=db,
                llm=llm,
                memory=memory,
                skills=skills,
                routing=routing,
                instructions_dir=str(instructions_dir),
                dedup=mock_dedup,
                rate_limiter=MagicMock(),
                tool_executor=AsyncMock(),
                context_assembler=AsyncMock(),
            )
        )

        msg = IncomingMessage(
            message_id="msg-no-channel-test",
            chat_id="test-chat",
            sender_id="user1",
            sender_name="Test User",
            text="Hello",
            timestamp=1000.0,
            acl_passed=True,
        )

        # Act — no channel passed
        await bot.handle_message(msg)

    await db.close()

    # Assert
    assert len(captured_messages) > 0, "LLM should have been called"
    system_msg = captured_messages[0]
    assert system_msg["role"] == "system"
    assert "WhatsApp" not in system_msg["content"], (
        "No WhatsApp prompt should appear without a channel"
    )
