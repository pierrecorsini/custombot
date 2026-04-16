"""
llm.py — OpenAI-compatible async LLM client.

Supports any provider that speaks the OpenAI Chat Completions API:
  OpenAI, Anthropic (via proxy), Ollama, LM Studio, OpenRouter, Groq, etc.

Just set base_url + api_key in config.json.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessage

from src.config import LLMConfig
from src.constants import WORKSPACE_DIR
from src.logging import get_correlation_id
from src.utils.retry import retry_with_backoff
from src.utils.type_guards import is_llm_config

if TYPE_CHECKING:
    from src.logging.llm_logging import LLMLogger

log = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token usage statistics for LLM API calls."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    request_count: int = 0

    def to_dict(self) -> Dict[str, int]:
        """Convert to dictionary for serialization."""
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "request_count": self.request_count,
        }

    _lock = threading.Lock()

    def add(self, prompt: int, completion: int) -> None:
        """Add token usage from a single request (thread-safe)."""
        with self._lock:
            self.prompt_tokens += prompt
            self.completion_tokens += completion
            self.total_tokens += prompt + completion
            self.request_count += 1


# Global token usage tracker for the session
_session_token_usage: TokenUsage = TokenUsage()


def get_token_usage() -> TokenUsage:
    """Get the global session token usage tracker."""
    return _session_token_usage


def reset_token_usage() -> None:
    """Reset the session token usage tracker."""
    global _session_token_usage
    _session_token_usage = TokenUsage()


class LLMClient:
    def __init__(self, cfg: LLMConfig, *, log_llm: bool = False) -> None:
        # Runtime validation for LLM config
        if not is_llm_config(cfg):
            raise ValueError(f"Invalid LLMConfig provided: {cfg!r}")
        self._cfg = cfg
        self._client = AsyncOpenAI(
            api_key=cfg.api_key or "sk-no-key",  # some local servers ignore it
            base_url=cfg.base_url,
        )
        self._llm_logger: LLMLogger | None = None
        if log_llm:
            from src.logging.llm_logging import LLMLogger

            log_dir = f"{WORKSPACE_DIR}/logs/llm"
            self._llm_logger = LLMLogger(log_dir)
            log.info("LLM request/response logging enabled → %s", log_dir)

    # ── public API ─────────────────────────────────────────────────────────

    @retry_with_backoff(max_retries=3, initial_delay=1.0)
    async def chat(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        timeout: Optional[float] = None,
    ) -> ChatCompletion:
        """
        Call the LLM and return the raw ChatCompletion object.

        The caller (bot.py) drives the ReAct loop: it checks `finish_reason`
        and appends tool results before calling again.

        Args:
            messages: List of message dicts
            tools: Optional list of tool definitions
            timeout: Optional timeout in seconds (default from config)
        """
        kwargs: Dict[str, Any] = {
            "model": self._cfg.model,
            "messages": messages,
            "temperature": self._cfg.temperature,
            "timeout": timeout if timeout is not None else self._cfg.timeout,
        }
        # Only include max_tokens if explicitly set (some APIs don't require it)
        if self._cfg.max_tokens is not None:
            kwargs["max_tokens"] = self._cfg.max_tokens
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        log.debug(
            "LLM request: model=%s, messages=%d, timeout=%s",
            self._cfg.model,
            len(messages),
            kwargs.get("timeout"),
        )

        # ── LLM file logging (request) ────────────────────────────────────
        request_ts = None
        if self._llm_logger:
            req_id = self._llm_logger.new_request_id()
            request_ts = self._llm_logger.log_request(
                request_id=req_id,
                model=self._cfg.model,
                messages=messages,
                tools=tools,
                temperature=self._cfg.temperature,
                max_tokens=self._cfg.max_tokens,
            )

        response = await self._client.chat.completions.create(**kwargs)

        # ── LLM file logging (response) ───────────────────────────────────
        if self._llm_logger and request_ts:
            self._llm_logger.log_response(
                request_id=req_id,
                model=self._cfg.model,
                response=response,
                request_ts=request_ts,
            )

        # Log token usage from response
        if response.usage:
            usage = response.usage
            # Handle both dict and object access patterns (API may return either)
            if isinstance(usage, dict):
                prompt_tokens = usage.get("prompt_tokens") or 0
                completion_tokens = usage.get("completion_tokens") or 0
                total_tokens = usage.get("total_tokens") or (
                    prompt_tokens + completion_tokens
                )
            else:
                prompt_tokens = usage.prompt_tokens or 0
                completion_tokens = usage.completion_tokens or 0
                total_tokens = usage.total_tokens or (prompt_tokens + completion_tokens)

            # Update session totals
            _session_token_usage.add(prompt_tokens, completion_tokens)

            # Get correlation ID for structured logging
            corr_id = get_correlation_id()

            # Per-request token usage → DEBUG to reduce log noise
            log.debug(
                "LLM token usage: model=%s prompt=%d completion=%d total=%d corr_id=%s",
                self._cfg.model,
                prompt_tokens,
                completion_tokens,
                total_tokens,
                corr_id or "none",
            )

            # Log cumulative session totals every 10 requests (reduces log I/O)
            if _session_token_usage.request_count % 10 == 0:
                log.info(
                    "LLM session totals: prompt=%d completion=%d total=%d requests=%d",
                    _session_token_usage.prompt_tokens,
                    _session_token_usage.completion_tokens,
                    _session_token_usage.total_tokens,
                    _session_token_usage.request_count,
                )

        log.debug(
            "LLM response: finish_reason=%s, tokens=%s",
            response.choices[0].finish_reason,
            response.usage,
        )
        return response

    @staticmethod
    def tool_call_to_dict(message: ChatCompletionMessage) -> Dict:
        """Convert a tool-call assistant message to a plain dict for context."""
        return {
            "role": "assistant",
            "content": message.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (message.tool_calls or [])
            ],
        }
