"""
conftest.py - Shared fixtures for E2E tests.

Provides:
- Mocked LLM responses (no real API calls)
- Temporary workspace directories (isolated per test)
- Test configuration files
- Mocked NeonizeBackend
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import (
    Config,
    LLMConfig,
    NeonizeConfig,
    WhatsAppConfig,
)

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: Temporary directories
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Create a temporary workspace directory for each test."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


@pytest.fixture
def temp_config_path(tmp_path: Path) -> Path:
    """Provide a temporary config file path."""
    return tmp_path / "config.json"


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: Configuration
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def test_config(temp_workspace: Path) -> Config:
    """Create a test configuration with mocked settings."""
    return Config(
        llm=LLMConfig(
            model="gpt-4o",
            base_url="https://api.openai.com/v1",
            api_key="sk-test-api-key-for-e2e-tests",
            temperature=0.7,
            max_tokens=100,
            system_prompt_prefix="You are a test assistant.",
        ),
        whatsapp=WhatsAppConfig(
            provider="neonize",
            neonize=NeonizeConfig(
                db_path=str(temp_workspace / "test_session.db"),
            ),
        ),
        memory_max_history=10,
        skills_auto_load=False,
    )


@pytest.fixture
def test_config_file(temp_config_path: Path, test_config: Config) -> Path:
    """Write test configuration to a temporary file."""
    with open(temp_config_path, "w", encoding="utf-8") as f:
        json.dump(asdict(test_config), f, indent=2)
    return temp_config_path


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: Mocked LLM
# ─────────────────────────────────────────────────────────────────────────────


class MockChatCompletion:
    """Mock ChatCompletion response from OpenAI API.

    Uses __init__ to create instance-level attributes, avoiding
    shared mutable class-level state that causes test pollution.
    """

    def __init__(self) -> None:
        self.choices = [self._make_choice()]
        self.usage: Dict[str, int] = {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }

    @staticmethod
    def _make_choice() -> Any:
        msg = MagicMock()
        msg.content = "Hello! I'm a test assistant. How can I help you today?"
        msg.tool_calls = None
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message = msg
        return choice


@pytest.fixture
def mock_llm_response() -> MockChatCompletion:
    """Provide a mock LLM response."""
    return MockChatCompletion()


@pytest.fixture
def mock_llm_client(mock_llm_response: MockChatCompletion):
    """Mock the LLMClient to return predefined responses."""
    with patch("llm.AsyncOpenAI") as mock_openai:
        mock_client = AsyncMock()
        mock_openai.return_value = mock_client

        # Mock the chat.completions.create method
        mock_client.chat = MagicMock()
        mock_client.chat.completions = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_llm_response)

        yield mock_client


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: Mocked NeonizeBackend
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_neonize_backend():
    """Mock the NeonizeBackend for tests that need WhatsApp connectivity."""
    from src.channels.neonize_backend import NeonizeBackend

    backend = MagicMock(spec=NeonizeBackend)
    backend.is_connected = False
    backend.connect = MagicMock()
    backend.disconnect = AsyncMock()
    backend.send = AsyncMock()
    backend.poll_message = AsyncMock(return_value=None)

    return backend


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: CLI Runner
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def cli_runner():
    """Provide a Click CLI test runner."""
    from click.testing import CliRunner

    return CliRunner()


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures: Routing Engine
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def mock_routing_engine(tmp_path: Path):
    """Create a mock RoutingEngine with a catch-all rule for tests."""
    from src.routing import RoutingEngine, RoutingRule

    # RoutingEngine now takes an instructions_dir path, not a Database
    engine = RoutingEngine(tmp_path)

    # Add a catch-all rule that matches everything and uses 'chat.agent.md'
    catch_all_rule = RoutingRule(
        id="test-catch-all",
        priority=100,
        sender="*",
        recipient="*",
        channel="*",
        content_regex="*",
        instruction="chat.agent.md",
        enabled=True,
        skillExecVerbose="",
    )
    engine._rules = [catch_all_rule]

    return engine
