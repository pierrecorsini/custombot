"""Mock LLM response factories.

Builds OpenAI-compatible response objects for every common scenario:
  - Simple text responses
  - Tool call responses (single and multiple)
  - Error responses (rate limit, timeout, context length)
  - Streaming chunks
  - Malformed responses (for error handling tests)

All factories return ``MagicMock`` objects that match the OpenAI
``ChatCompletion`` schema.

Usage::

    from tests.mocks.llm_responses import make_text_response, make_tool_call

    response = make_text_response("Hello world")
    tool = make_tool_call(name="search", arguments='{"q": "test"}')
"""

from __future__ import annotations

import json as _json
from typing import Any

from unittest.mock import MagicMock


# ── Usage stats ─────────────────────────────────────────────────────────────


def make_usage(
    prompt_tokens: int = 10,
    completion_tokens: int = 20,
    total_tokens: int = 30,
) -> MagicMock:
    """Build a mock ``usage`` object."""
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    usage.total_tokens = total_tokens
    return usage


# ── Tool calls ──────────────────────────────────────────────────────────────


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


# ── Core response builders ─────────────────────────────────────────────────


def make_chat_response(
    *,
    content: str | None = "Hello back!",
    finish_reason: str = "stop",
    tool_calls: list[MagicMock] | None = None,
    usage: MagicMock | None = None,
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


def make_text_response(
    text: str,
    *,
    prompt_tokens: int = 10,
    completion_tokens: int | None = None,
) -> MagicMock:
    """Build a mock ChatCompletion with a plain-text stop response."""
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


def make_tool_call_response(
    tool_name: str = "echo",
    tool_args: dict | None = None,
    tool_call_id: str = "call_tc_001",
    *,
    usage: MagicMock | None = None,
) -> MagicMock:
    """Build a ChatCompletion that requests a single tool call."""
    tc = make_tool_call(
        call_id=tool_call_id,
        name=tool_name,
        arguments=_json.dumps(tool_args or {}),
    )
    return make_chat_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[tc],
        usage=usage or make_usage(prompt_tokens=15, completion_tokens=10, total_tokens=25),
    )


def make_multi_tool_call_response(
    calls: list[dict],
    *,
    usage: MagicMock | None = None,
) -> MagicMock:
    """Build a ChatCompletion that requests multiple tool calls.

    Parameters
    ----------
    calls:
        List of dicts with keys ``name``, ``args`` (dict), ``call_id`` (optional).
    """
    tool_calls = []
    for i, call in enumerate(calls):
        tc = make_tool_call(
            call_id=call.get("call_id", f"call_multi_{i:03d}"),
            name=call["name"],
            arguments=_json.dumps(call.get("args", {})),
        )
        tool_calls.append(tc)
    return make_chat_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=tool_calls,
        usage=usage or make_usage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
    )


# ── Error response factories ───────────────────────────────────────────────


def make_rate_limit_response() -> MagicMock:
    """Build a response simulating a rate-limit error (finish_reason='length').

    In practice rate limits throw exceptions — this is for testing code
    that handles the ``length`` finish reason.
    """
    return make_chat_response(
        content=None,
        finish_reason="length",
        usage=make_usage(prompt_tokens=100, completion_tokens=0, total_tokens=100),
    )


def make_context_overflow_response() -> MagicMock:
    """Build a response indicating context length was exceeded."""
    return make_chat_response(
        content=None,
        finish_reason="length",
        usage=make_usage(prompt_tokens=128000, completion_tokens=0, total_tokens=128000),
    )


def make_timeout_response() -> MagicMock:
    """Build a minimal response for timeout scenarios (empty content)."""
    return make_chat_response(
        content="",
        finish_reason="stop",
        usage=make_usage(prompt_tokens=10, completion_tokens=0, total_tokens=10),
    )


# ── Streaming response factories ───────────────────────────────────────────


def make_streaming_response(
    *,
    chunks: list[str] | None = None,
    finish_reason: str = "stop",
    usage: MagicMock | None = None,
) -> list[MagicMock]:
    """Build a list of mock streaming chunks simulating an OpenAI stream.

    Parameters
    ----------
    chunks:
        Text fragments to deliver.  Defaults to ``["Hello", " world", "!"]``.
    finish_reason:
        The ``finish_reason`` on the final chunk.
    usage:
        If provided, attached to the final chunk.
    """
    chunks = chunks or ["Hello", " world", "!"]
    events: list[MagicMock] = []

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


def make_streaming_tool_call_chunks(
    tool_name: str = "search",
    arguments: str = '{"q": "test"}',
    call_id: str = "call_stream_001",
) -> list[MagicMock]:
    """Build streaming chunks that simulate a tool call in progress."""
    events: list[MagicMock] = []

    # First chunk: tool call starts
    delta1 = MagicMock()
    delta1.content = None
    tc_delta = MagicMock()
    tc_delta.id = call_id
    tc_delta.function.name = tool_name
    tc_delta.function.arguments = ""
    tc_delta.type = "function"
    delta1.tool_calls = [tc_delta]

    choice1 = MagicMock()
    choice1.delta = delta1
    choice1.finish_reason = None
    event1 = MagicMock()
    event1.choices = [choice1]
    events.append(event1)

    # Second chunk: arguments streaming
    delta2 = MagicMock()
    delta2.content = None
    tc_delta2 = MagicMock()
    tc_delta2.id = None
    tc_delta2.function.name = None
    tc_delta2.function.arguments = arguments
    tc_delta2.type = None
    delta2.tool_calls = [tc_delta2]

    choice2 = MagicMock()
    choice2.delta = delta2
    choice2.finish_reason = "tool_calls"
    event2 = MagicMock()
    event2.choices = [choice2]
    events.append(event2)

    return events


# ── Malformed response factories (error handling tests) ─────────────────────


def make_malformed_empty_choices() -> MagicMock:
    """Build a response with empty choices list."""
    response = MagicMock()
    response.choices = []
    response.usage = make_usage()
    return response


def make_malformed_no_content() -> MagicMock:
    """Build a response where message.content is None and no tool_calls."""
    return make_chat_response(
        content=None,
        finish_reason="stop",
        usage=make_usage(),
    )


def make_malformed_bad_json_tool_args() -> MagicMock:
    """Build a tool call response with invalid JSON arguments."""
    tc = make_tool_call(
        call_id="call_bad_args",
        name="broken_tool",
        arguments="{invalid json!!!",
    )
    return make_chat_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[tc],
    )


def make_malformed_missing_function_name() -> MagicMock:
    """Build a tool call response where function.name is empty."""
    tc = MagicMock()
    tc.id = "call_no_name"
    tc.type = "function"
    tc.function.name = ""
    tc.function.arguments = "{}"
    return make_chat_response(
        content=None,
        finish_reason="tool_calls",
        tool_calls=[tc],
    )
