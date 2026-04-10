"""
Tests for src/llm.py — OpenAI-compatible async LLM client.

Unit tests covering:
  - TokenUsage dataclass: defaults, add(), to_dict()
  - Global session token usage: get_token_usage(), reset_token_usage()
  - LLMClient.__init__: config validation, AsyncOpenAI construction, log_llm mode
  - LLMClient.chat: async API call, kwargs building, token tracking, tool support
  - LLMClient.chat: retry_with_backoff decorator integration
  - LLMClient.chat: LLM file logging (request/response)
  - LLMClient.tool_call_to_dict: static conversion of tool-call messages
  - Error handling and edge cases
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import LLMConfig
from src.llm import (
    LLMClient,
    TokenUsage,
    get_token_usage,
    reset_token_usage,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def valid_cfg() -> LLMConfig:
    """Provide a valid LLMConfig for tests."""
    return LLMConfig(
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="sk-test-key",
        temperature=0.5,
        max_tokens=256,
        timeout=30.0,
        system_prompt_prefix="You are a test assistant.",
        max_tool_iterations=5,
    )


@pytest.fixture
def valid_cfg_no_max_tokens() -> LLMConfig:
    """Provide a valid LLMConfig without max_tokens set."""
    return LLMConfig(
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="sk-test-key",
        temperature=0.5,
        max_tokens=None,
        timeout=30.0,
        system_prompt_prefix="",
        max_tool_iterations=5,
    )


@pytest.fixture
def valid_cfg_no_api_key() -> LLMConfig:
    """Provide a valid LLMConfig with no API key (local server)."""
    return LLMConfig(
        model="llama3",
        base_url="http://localhost:11434/v1",
        api_key="",
        temperature=0.7,
        max_tokens=None,
        timeout=60.0,
        system_prompt_prefix="",
        max_tool_iterations=10,
    )


def _make_mock_usage(
    prompt_tokens: int = 50,
    completion_tokens: int = 100,
    total_tokens: int = 150,
    as_dict: bool = False,
) -> Any:
    """Create a mock usage object for ChatCompletion responses."""
    if as_dict:
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens
    return usage


def _make_mock_chat_completion(
    *,
    content: str = "Hello!",
    finish_reason: str = "stop",
    tool_calls: Optional[list] = None,
    usage: Any = None,
) -> MagicMock:
    """Create a mock ChatCompletion response."""
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls

    choice = MagicMock()
    choice.message = message
    choice.finish_reason = finish_reason

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def _make_mock_tool_call(
    call_id: str = "call_abc123",
    name: str = "get_weather",
    arguments: str = '{"location": "Tokyo"}',
) -> MagicMock:
    """Create a mock tool call object."""
    tc = MagicMock()
    tc.id = call_id
    tc.type = "function"
    tc.function = MagicMock()
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


@pytest.fixture(autouse=True)
def _reset_global_token_usage():
    """Reset global token usage before and after each test."""
    reset_token_usage()
    yield
    reset_token_usage()


# ─────────────────────────────────────────────────────────────────────────────
# TokenUsage dataclass
# ─────────────────────────────────────────────────────────────────────────────


class TestTokenUsageDefaults:
    """Tests for TokenUsage default field values."""

    def test_default_values(self):
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.request_count == 0


class TestTokenUsageAdd:
    """Tests for TokenUsage.add() method."""

    def test_add_single_request(self):
        usage = TokenUsage()
        usage.add(prompt=10, completion=20)
        assert usage.prompt_tokens == 10
        assert usage.completion_tokens == 20
        assert usage.total_tokens == 30
        assert usage.request_count == 1

    def test_add_accumulates_across_calls(self):
        usage = TokenUsage()
        usage.add(prompt=10, completion=20)
        usage.add(prompt=30, completion=40)
        assert usage.prompt_tokens == 40
        assert usage.completion_tokens == 60
        assert usage.total_tokens == 100
        assert usage.request_count == 2

    def test_add_zero_tokens(self):
        usage = TokenUsage()
        usage.add(prompt=0, completion=0)
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0
        assert usage.request_count == 1

    def test_add_large_values(self):
        usage = TokenUsage()
        usage.add(prompt=100_000, completion=50_000)
        assert usage.total_tokens == 150_000
        assert usage.request_count == 1

    def test_add_many_requests(self):
        usage = TokenUsage()
        for i in range(100):
            usage.add(prompt=1, completion=1)
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 100
        assert usage.total_tokens == 200
        assert usage.request_count == 100


class TestTokenUsageToDict:
    """Tests for TokenUsage.to_dict() method."""

    def test_default_to_dict(self):
        usage = TokenUsage()
        result = usage.to_dict()
        assert result == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
        }

    def test_after_add_to_dict(self):
        usage = TokenUsage()
        usage.add(prompt=10, completion=20)
        result = usage.to_dict()
        assert result == {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "request_count": 1,
        }

    def test_to_dict_returns_plain_dict(self):
        usage = TokenUsage()
        result = usage.to_dict()
        assert isinstance(result, dict)

    def test_to_dict_independent_copy(self):
        """Modifying the returned dict should not affect the dataclass."""
        usage = TokenUsage()
        usage.add(prompt=5, completion=5)
        result = usage.to_dict()
        result["prompt_tokens"] = 999
        assert usage.prompt_tokens == 5


# ─────────────────────────────────────────────────────────────────────────────
# Global session token usage
# ─────────────────────────────────────────────────────────────────────────────


class TestGetTokenUsage:
    """Tests for get_token_usage() global tracker."""

    def test_returns_token_usage_instance(self):
        result = get_token_usage()
        assert isinstance(result, TokenUsage)

    def test_returns_same_instance(self):
        """get_token_usage() should always return the same global object."""
        a = get_token_usage()
        b = get_token_usage()
        assert a is b


class TestResetTokenUsage:
    """Tests for reset_token_usage() function."""

    def test_resets_to_defaults(self):
        usage = get_token_usage()
        usage.add(prompt=100, completion=200)
        reset_token_usage()
        fresh = get_token_usage()
        assert fresh.prompt_tokens == 0
        assert fresh.completion_tokens == 0
        assert fresh.total_tokens == 0
        assert fresh.request_count == 0

    def test_creates_new_instance(self):
        """reset_token_usage() should create a fresh TokenUsage object."""
        old = get_token_usage()
        old.add(prompt=10, completion=10)
        reset_token_usage()
        new = get_token_usage()
        assert old is not new

    def test_get_after_reset_is_fresh(self):
        get_token_usage().add(prompt=50, completion=50)
        reset_token_usage()
        usage = get_token_usage()
        assert usage.request_count == 0
        assert usage.total_tokens == 0


# ─────────────────────────────────────────────────────────────────────────────
# LLMClient.__init__
# ─────────────────────────────────────────────────────────────────────────────


class TestLLMClientInit:
    """Tests for LLMClient initialization."""

    @patch("src.llm.AsyncOpenAI")
    def test_creates_client_with_valid_config(self, mock_openai, valid_cfg):
        client = LLMClient(valid_cfg)
        mock_openai.assert_called_once_with(
            api_key="sk-test-key",
            base_url="https://api.openai.com/v1",
        )
        assert client._cfg is valid_cfg

    @patch("src.llm.AsyncOpenAI")
    def test_uses_no_key_fallback_when_api_key_empty(
        self, mock_openai, valid_cfg_no_api_key
    ):
        LLMClient(valid_cfg_no_api_key)
        mock_openai.assert_called_once_with(
            api_key="sk-no-key",
            base_url="http://localhost:11434/v1",
        )

    @patch("src.llm.AsyncOpenAI")
    def test_raises_on_invalid_config(self, mock_openai):
        """Passing a non-LLMConfig object should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid LLMConfig"):
            LLMClient("not a config")  # type: ignore[arg-type]

    @patch("src.llm.AsyncOpenAI")
    def test_raises_on_config_with_empty_model(self, mock_openai):
        """is_llm_config returns False when model is empty string."""
        bad_cfg = LLMConfig(model="", base_url="https://api.openai.com/v1")
        with pytest.raises(ValueError, match="Invalid LLMConfig"):
            LLMClient(bad_cfg)

    @patch("src.llm.AsyncOpenAI")
    def test_llm_logger_disabled_by_default(self, mock_openai, valid_cfg):
        client = LLMClient(valid_cfg)
        assert client._llm_logger is None

    @patch("src.llm.AsyncOpenAI")
    @patch("src.logging.llm_logging.LLMLogger")
    @patch("src.llm.WORKSPACE_DIR", "/tmp/test_workspace")
    def test_llm_logger_enabled_when_requested(
        self, mock_logger_cls, mock_openai, valid_cfg
    ):
        client = LLMClient(valid_cfg, log_llm=True)
        assert client._llm_logger is not None
        mock_logger_cls.assert_called_once_with("/tmp/test_workspace/logs/llm")

    @patch("src.llm.AsyncOpenAI")
    def test_log_llm_false_does_not_import_logger(self, mock_openai, valid_cfg):
        """When log_llm=False, LLMLogger should not be imported/initialized."""
        client = LLMClient(valid_cfg, log_llm=False)
        assert client._llm_logger is None


# ─────────────────────────────────────────────────────────────────────────────
# LLMClient.chat
# ─────────────────────────────────────────────────────────────────────────────


class TestLLMClientChat:
    """Tests for LLMClient.chat() async method."""

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_basic_chat_call(self, mock_openai, valid_cfg):
        """chat() should call the OpenAI API and return the response."""
        mock_response = _make_mock_chat_completion(
            content="Hi there!", usage=_make_mock_usage(10, 20, 30)
        )
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        messages = [{"role": "user", "content": "Hello"}]
        result = await client.chat(messages)

        assert result is mock_response
        mock_client_instance.chat.completions.create.assert_called_once()

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_passes_correct_kwargs(self, mock_openai, valid_cfg):
        """chat() should pass model, messages, temperature, max_tokens, timeout."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        messages = [{"role": "user", "content": "Hello"}]
        await client.chat(messages, timeout=45.0)

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["model"] == "gpt-4o"
        assert call_kwargs["messages"] == messages
        assert call_kwargs["temperature"] == 0.5
        assert call_kwargs["max_tokens"] == 256
        assert call_kwargs["timeout"] == 45.0

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_uses_config_timeout_when_none(self, mock_openai, valid_cfg):
        """When timeout is None, should use config.timeout."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}], timeout=None)

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["timeout"] == 30.0

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_omits_max_tokens_when_none(
        self, mock_openai, valid_cfg_no_max_tokens
    ):
        """When max_tokens is None, it should NOT be in the API kwargs."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg_no_max_tokens)
        await client.chat([{"role": "user", "content": "Hi"}])

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert "max_tokens" not in call_kwargs

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_includes_max_tokens_when_set(self, mock_openai, valid_cfg):
        """When max_tokens is set, it should be in the API kwargs."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["max_tokens"] == 256

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_with_tools(self, mock_openai, valid_cfg):
        """chat() should include tools and tool_choice when tools are provided."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        tools = [{"type": "function", "function": {"name": "test_func"}}]
        await client.chat(
            [{"role": "user", "content": "Use tool"}],
            tools=tools,
        )

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert call_kwargs["tools"] == tools
        assert call_kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_without_tools_omits_tool_fields(self, mock_openai, valid_cfg):
        """chat() should NOT include tools/tool_choice when no tools given."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert "tools" not in call_kwargs
        assert "tool_choice" not in call_kwargs

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    async def test_chat_with_empty_tools_list(self, mock_openai, valid_cfg):
        """An empty tools list should be treated as no tools (falsy)."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}], tools=[])

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert "tools" not in call_kwargs
        assert "tool_choice" not in call_kwargs


# ─────────────────────────────────────────────────────────────────────────────
# Token tracking from response.usage
# ─────────────────────────────────────────────────────────────────────────────


class TestChatTokenTracking:
    """Tests for token usage tracking in LLMClient.chat()."""

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-123")
    async def test_updates_session_usage_from_object(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Token usage should be tracked from response.usage (object form)."""
        usage_obj = _make_mock_usage(
            prompt_tokens=50, completion_tokens=100, total_tokens=150
        )
        mock_response = _make_mock_chat_completion(usage=usage_obj)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.prompt_tokens == 50
        assert session.completion_tokens == 100
        assert session.total_tokens == 150
        assert session.request_count == 1

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value=None)
    async def test_updates_session_usage_from_dict(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Token usage should be tracked from response.usage (dict form)."""
        usage_dict = _make_mock_usage(
            prompt_tokens=25, completion_tokens=75, total_tokens=100, as_dict=True
        )
        mock_response = _make_mock_chat_completion(usage=usage_dict)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.prompt_tokens == 25
        assert session.completion_tokens == 75
        assert session.total_tokens == 100
        assert session.request_count == 1

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-xyz")
    async def test_accumulates_across_multiple_calls(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Token usage should accumulate across multiple chat() calls."""
        mock_client_instance = MagicMock()
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)

        # First call
        resp1 = _make_mock_chat_completion(usage=_make_mock_usage(10, 20, 30))
        mock_client_instance.chat.completions.create = AsyncMock(return_value=resp1)
        await client.chat([{"role": "user", "content": "One"}])

        # Second call
        resp2 = _make_mock_chat_completion(usage=_make_mock_usage(40, 60, 100))
        mock_client_instance.chat.completions.create = AsyncMock(return_value=resp2)
        await client.chat([{"role": "user", "content": "Two"}])

        session = get_token_usage()
        assert session.prompt_tokens == 50
        assert session.completion_tokens == 80
        assert session.total_tokens == 130
        assert session.request_count == 2

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value=None)
    async def test_no_tracking_when_usage_is_none(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """If response.usage is None, session usage should not be updated."""
        mock_response = _make_mock_chat_completion(usage=None)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.request_count == 0
        assert session.total_tokens == 0

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="c1")
    async def test_handles_zero_token_fields(self, mock_corr, mock_openai, valid_cfg):
        """Token fields that are 0 should still be tracked (not treated as None)."""
        usage_obj = _make_mock_usage(
            prompt_tokens=0, completion_tokens=0, total_tokens=0
        )
        mock_response = _make_mock_chat_completion(usage=usage_obj)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.prompt_tokens == 0
        assert session.completion_tokens == 0
        assert session.total_tokens == 0
        assert session.request_count == 1

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="c2")
    async def test_dict_usage_computes_total_when_missing(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Dict usage without total_tokens should compute it from prompt + completion."""
        usage_dict = {"prompt_tokens": 30, "completion_tokens": 70}
        mock_response = _make_mock_chat_completion(usage=usage_dict)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.prompt_tokens == 30
        assert session.completion_tokens == 70
        assert session.total_tokens == 100
        assert session.request_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# LLM file logging
# ─────────────────────────────────────────────────────────────────────────────


class TestChatLLMLogging:
    """Tests for LLM file logging within chat()."""

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-log")
    async def test_logs_request_and_response_when_enabled(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """When log_llm=True, both request and response should be logged."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        with patch("src.logging.llm_logging.LLMLogger") as mock_logger_cls:
            mock_logger = MagicMock()
            mock_logger.new_request_id.return_value = "req-001"
            mock_logger.log_request.return_value = "2026-01-01T00:00:00"
            mock_logger_cls.return_value = mock_logger

            with patch("src.llm.WORKSPACE_DIR", "/tmp/ws"):
                client = LLMClient(valid_cfg, log_llm=True)

            messages = [{"role": "user", "content": "Hello"}]
            tools = [{"type": "function", "function": {"name": "f"}}]
            await client.chat(messages, tools=tools)

            # Verify request logging
            mock_logger.new_request_id.assert_called_once()
            mock_logger.log_request.assert_called_once()
            req_call = mock_logger.log_request.call_args
            assert req_call[1]["model"] == "gpt-4o"
            assert req_call[1]["messages"] == messages
            assert req_call[1]["tools"] == tools

            # Verify response logging
            mock_logger.log_response.assert_called_once()
            resp_call = mock_logger.log_response.call_args
            assert resp_call[1]["request_id"] == "req-001"
            assert resp_call[1]["model"] == "gpt-4o"
            assert resp_call[1]["response"] is mock_response

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-nolog")
    async def test_no_logging_when_disabled(self, mock_corr, mock_openai, valid_cfg):
        """When log_llm=False (default), no logging methods should be called."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        assert client._llm_logger is None


# ─────────────────────────────────────────────────────────────────────────────
# Retry behavior (retry_with_backoff decorator)
# ─────────────────────────────────────────────────────────────────────────────


class TestChatRetryBehavior:
    """Tests verifying retry_with_backoff decorator on chat()."""

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-retry")
    async def test_succeeds_on_first_attempt(self, mock_corr, mock_openai, valid_cfg):
        """When the first call succeeds, no retries should occur."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_create = AsyncMock(return_value=mock_response)
        mock_client_instance.chat.completions.create = mock_create
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        result = await client.chat([{"role": "user", "content": "Hi"}])

        assert result is mock_response
        assert mock_create.call_count == 1

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-retry2")
    @patch("src.llm.asyncio.sleep", new_callable=AsyncMock)
    async def test_retries_on_transient_error(
        self, mock_sleep, mock_corr, mock_openai, valid_cfg
    ):
        """chat() should retry on transient errors (e.g. rate limit)."""
        from openai import RateLimitError

        rate_limit_err = RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429),
            body=None,
        )

        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_create = AsyncMock(
            side_effect=[rate_limit_err, rate_limit_err, mock_response]
        )
        mock_client_instance.chat.completions.create = mock_create
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        result = await client.chat([{"role": "user", "content": "Hi"}])

        assert result is mock_response
        assert mock_create.call_count == 3
        # sleep should have been called for each retry (2 retries before success)
        assert mock_sleep.call_count == 2

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-retry3")
    @patch("src.llm.asyncio.sleep", new_callable=AsyncMock)
    async def test_raises_after_max_retries_exhausted(
        self, mock_sleep, mock_corr, mock_openai, valid_cfg
    ):
        """chat() should raise after 3 retries on persistent transient errors."""
        from openai import RateLimitError

        rate_limit_err = RateLimitError(
            message="Rate limit exceeded",
            response=MagicMock(status_code=429),
            body=None,
        )

        mock_client_instance = MagicMock()
        mock_create = AsyncMock(side_effect=rate_limit_err)
        mock_client_instance.chat.completions.create = mock_create
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        with pytest.raises(RateLimitError):
            await client.chat([{"role": "user", "content": "Hi"}])

        # 1 initial + 3 retries = 4 total calls
        assert mock_create.call_count == 4

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="corr-noretry")
    async def test_does_not_retry_on_non_transient_error(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Non-transient errors should be raised immediately without retry."""
        from openai import BadRequestError

        bad_request_err = BadRequestError(
            message="Invalid request",
            response=MagicMock(status_code=400),
            body=None,
        )

        mock_client_instance = MagicMock()
        mock_create = AsyncMock(side_effect=bad_request_err)
        mock_client_instance.chat.completions.create = mock_create
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        with pytest.raises(BadRequestError):
            await client.chat([{"role": "user", "content": "Hi"}])

        assert mock_create.call_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# tool_call_to_dict
# ─────────────────────────────────────────────────────────────────────────────


class TestToolCallToDict:
    """Tests for LLMClient.tool_call_to_dict() static method."""

    def test_converts_single_tool_call(self):
        tc = _make_mock_tool_call(
            call_id="call_001", name="get_weather", arguments='{"city": "Paris"}'
        )
        message = MagicMock()
        message.content = "Let me check the weather."
        message.tool_calls = [tc]

        result = LLMClient.tool_call_to_dict(message)

        assert result == {
            "role": "assistant",
            "content": "Let me check the weather.",
            "tool_calls": [
                {
                    "id": "call_001",
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "arguments": '{"city": "Paris"}',
                    },
                }
            ],
        }

    def test_converts_multiple_tool_calls(self):
        tc1 = _make_mock_tool_call(
            call_id="call_1", name="func_a", arguments='{"x": 1}'
        )
        tc2 = _make_mock_tool_call(
            call_id="call_2", name="func_b", arguments='{"y": 2}'
        )
        message = MagicMock()
        message.content = None
        message.tool_calls = [tc1, tc2]

        result = LLMClient.tool_call_to_dict(message)

        assert len(result["tool_calls"]) == 2
        assert result["tool_calls"][0]["id"] == "call_1"
        assert result["tool_calls"][1]["id"] == "call_2"
        assert result["content"] is None

    def test_empty_tool_calls_list(self):
        """Message with tool_calls=[] should produce empty list in output."""
        message = MagicMock()
        message.content = "No tools needed."
        message.tool_calls = []

        result = LLMClient.tool_call_to_dict(message)

        assert result == {
            "role": "assistant",
            "content": "No tools needed.",
            "tool_calls": [],
        }

    def test_none_tool_calls(self):
        """Message with tool_calls=None should produce empty list in output."""
        message = MagicMock()
        message.content = "Just text."
        message.tool_calls = None

        result = LLMClient.tool_call_to_dict(message)

        assert result == {
            "role": "assistant",
            "content": "Just text.",
            "tool_calls": [],
        }

    def test_preserves_complex_arguments(self):
        """Complex JSON arguments should be preserved as-is."""
        complex_args = (
            '{"filters": {"date": "2024-01-01", "tags": ["a", "b"]}, "limit": 10}'
        )
        tc = _make_mock_tool_call(
            call_id="call_complex", name="search", arguments=complex_args
        )
        message = MagicMock()
        message.content = "Searching..."
        message.tool_calls = [tc]

        result = LLMClient.tool_call_to_dict(message)

        assert result["tool_calls"][0]["function"]["arguments"] == complex_args

    def test_is_static_method(self):
        """tool_call_to_dict should be callable without an instance."""
        message = MagicMock()
        message.content = "test"
        message.tool_calls = None
        # Should not raise
        result = LLMClient.tool_call_to_dict(message)
        assert isinstance(result, dict)


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Edge case tests for LLMClient."""

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="edge-1")
    async def test_returns_response_even_without_usage(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """chat() should return the response even when usage is None."""
        mock_response = _make_mock_chat_completion(
            content="Done!", finish_reason="stop", usage=None
        )
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        result = await client.chat([{"role": "user", "content": "Hi"}])

        assert result is mock_response
        assert result.choices[0].message.content == "Done!"

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="edge-2")
    async def test_multiple_messages_sent(self, mock_corr, mock_openai, valid_cfg):
        """chat() should pass through all messages to the API."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage())
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi!"},
            {"role": "user", "content": "How are you?"},
        ]
        await client.chat(messages)

        call_kwargs = mock_client_instance.chat.completions.create.call_args[1]
        assert len(call_kwargs["messages"]) == 4

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="edge-3")
    async def test_session_usage_independent_across_clients(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Token usage is global — two clients share the same session tracker."""
        mock_response = _make_mock_chat_completion(usage=_make_mock_usage(10, 20, 30))
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client1 = LLMClient(valid_cfg)
        client2 = LLMClient(valid_cfg)

        await client1.chat([{"role": "user", "content": "From client 1"}])
        await client2.chat([{"role": "user", "content": "From client 2"}])

        session = get_token_usage()
        # Both calls should be tracked
        assert session.request_count == 2
        assert session.prompt_tokens == 20  # 10 + 10
        assert session.completion_tokens == 40  # 20 + 20

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="edge-4")
    async def test_dict_usage_with_none_token_fields(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Dict usage with None token fields should default to 0."""
        usage_dict = {
            "prompt_tokens": None,
            "completion_tokens": None,
            "total_tokens": None,
        }
        mock_response = _make_mock_chat_completion(usage=usage_dict)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.prompt_tokens == 0
        assert session.completion_tokens == 0
        assert session.total_tokens == 0
        assert session.request_count == 1

    @pytest.mark.asyncio
    @patch("src.llm.AsyncOpenAI")
    @patch("src.llm.get_correlation_id", return_value="edge-5")
    async def test_object_usage_with_none_token_fields(
        self, mock_corr, mock_openai, valid_cfg
    ):
        """Object usage with None token fields should default to 0."""
        usage_obj = MagicMock()
        usage_obj.prompt_tokens = None
        usage_obj.completion_tokens = None
        usage_obj.total_tokens = None
        mock_response = _make_mock_chat_completion(usage=usage_obj)
        mock_client_instance = MagicMock()
        mock_client_instance.chat.completions.create = AsyncMock(
            return_value=mock_response
        )
        mock_openai.return_value = mock_client_instance

        client = LLMClient(valid_cfg)
        await client.chat([{"role": "user", "content": "Hi"}])

        session = get_token_usage()
        assert session.prompt_tokens == 0
        assert session.completion_tokens == 0
        assert session.total_tokens == 0
        assert session.request_count == 1
