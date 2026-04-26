"""
Shared LLM mock helpers for tests.

Provides reusable builders for OpenAI-compatible response objects,
eliminating per-file reinvention of mock responses.

Usage::

    from tests.helpers.llm_mocks import (
        MockChatCompletion,
        make_chat_response,
        make_text_response,
        make_tool_call,
        make_tool_call_response,
        make_usage,
        make_streaming_response,
    )
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from unittest.mock import MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# MockChatCompletion — backward-compatible class from conftest.py
# ─────────────────────────────────────────────────────────────────────────────


class MockChatCompletion:
    """Mock ChatCompletion response from OpenAI API.

    Uses ``__init__`` to create instance-level attributes, avoiding
    shared mutable class-level state that causes test pollution.
    """

    def __init__(self) -> None:
        self.choices = [self._make_choice()]

    @staticmethod
    def _make_choice() -> Any:
        msg = MagicMock()
        msg.content = "Hello! I'm a test assistant. How can I help you today?"
        msg.tool_calls = None
        choice = MagicMock()
        choice.finish_reason = "stop"
        choice.message = msg
        return choice

    usage: Dict[str, int] = {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Builder helpers
# ─────────────────────────────────────────────────────────────────────────────


def make_usage(
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    total_tokens: int = 30,
) -> MagicMock:
    """Build a mock ``usage`` object for ChatCompletion responses."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens
    return usage


def make_tool_call(
    call_id: str = "call_001",
    name: str = "web_search",
    arguments: str = '{"query": "test"}',
) -> MagicMock:
    """Build a mock tool-call object matching the OpenAI schema."""
    tc = MagicMock()
    tc.id = call_id
    tc.type = "function"
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def make_chat_response(
    *,
    content: str = "Hello back!",
    finish_reason: str = "stop",
    tool_calls: Optional[List[MagicMock]] = None,
    usage: Optional[MagicMock] = None,
) -> MagicMock:
    """Build a mock OpenAI ChatCompletion response.

    Parameters
    ----------
    content:
        The ``message.content`` value.
    finish_reason:
        Usually ``"stop"`` or ``"tool_calls"``.
    tool_calls:
        List of mock tool-call objects (use :func:`make_tool_call`).
    usage:
        Mock usage object (use :func:`make_usage`).  ``None`` omits it.
    """
    message = MagicMock()
    message.content = content
    message.tool_calls = tool_calls

    choice = MagicMock()
    choice.finish_reason = finish_reason
    choice.message = message

    response = MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


def make_tool_call_response(
    tool_name: str = "echo",
    tool_args: Optional[dict] = None,
    tool_call_id: str = "call_tc_001",
    *,
    usage: Optional[MagicMock] = None,
) -> MagicMock:
    """Build a ChatCompletion that requests a single tool call.

    Convenience wrapper around :func:`make_chat_response` pre-configured
    with ``finish_reason="tool_calls"`` and a single tool call.
    """
    import json

    tc = make_tool_call(
        call_id=tool_call_id,
        name=tool_name,
        arguments=json.dumps(tool_args or {}),
    )
    return make_chat_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[tc],
        usage=usage or make_usage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
    )


def make_streaming_response(
    *,
    chunks: Optional[List[str]] = None,
    finish_reason: str = "stop",
    usage: Optional[MagicMock] = None,
) -> List[MagicMock]:
    """Build a list of mock streaming chunks simulating an OpenAI stream.

    Parameters
    ----------
    chunks:
        Text fragments to deliver.  Defaults to ``["Hello", " world", "!"]``.
    finish_reason:
        The ``finish_reason`` on the final chunk.
    usage:
        If provided, attached to the final chunk.

    Returns
    -------
    list[MagicMock]
        Iterable of mock stream-event objects with ``.choices[0].delta.content``.
    """
    chunks = chunks or ["Hello", " world", "!"]
    events: List[MagicMock] = []

    for i, text in enumerate(chunks):
        is_last = i == len(chunks) - 1
        delta = MagicMock()
        delta.content = text
        delta.tool_calls = None

        choice = MagicMock()
        choice.delta = delta
        choice.finish_reason = finish_reason if is_last else None

        event = MagicMock()
        event.choices = [choice]
        event.usage = usage if is_last else None
        events.append(event)

    return events


def make_text_response(
    text: str,
    *,
    prompt_tokens: int = 10,
    completion_tokens: Optional[int] = None,
) -> MagicMock:
    """Build a mock ChatCompletion with a plain-text stop response.

    Convenience wrapper around :func:`make_chat_response` for the common
    case of a non-tool-call text reply.

    Parameters
    ----------
    text:
        The ``message.content`` value.
    prompt_tokens:
        Mock prompt token count (default 10).
    completion_tokens:
        Mock completion token count.  Defaults to ``max(1, len(text) // 4)``.
    """
    ct = completion_tokens if completion_tokens is not None else max(1, len(text) // 4)
    return make_chat_response(
        content=text,
        finish_reason="stop",
        usage=make_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=ct,
            total_tokens=prompt_tokens + ct,
        ),
    )
