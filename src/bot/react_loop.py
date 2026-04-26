"""
react_loop.py — ReAct (Reason + Act) loop implementation.

Implements the iterative LLM↔tool-call cycle:
    call LLM → if tool_calls → execute tools → append results → loop
    call LLM → if stop → return text

Extracted from ``Bot`` to isolate the loop's complexity (retry, parallel
tool execution, iteration cap, streaming) from message-level orchestration.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable

from openai.types.chat.chat_completion import Choice
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall,
)
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionMessageParam,
    ChatCompletionToolMessageParam,
    ChatCompletionToolParam,
)

from src.channels.base import SendMediaCallback
from src.constants import MAX_TOOL_CALLS_PER_TURN, MAX_TOOL_RESULT_PERSIST_LENGTH, WORKSPACE_DIR
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import Event, get_event_bus
from src.core.serialization import serialize_tool_call_message
from src.core.tool_formatter import ToolLogEntry, format_single_tool_execution
from src.core.tool_executor import ToolExecutor
from src.exceptions import ErrorCode, LLMError
from src.logging import get_correlation_id
from src.monitoring import PerformanceMetrics
from src.utils import JSONDecodeError

if TYPE_CHECKING:
    from src.channels.base import BaseChannel
    from src.llm_provider import LLMProvider

log = logging.getLogger(__name__)

# Type alias for streaming tool execution updates
StreamCallback = Callable[[str], Awaitable[None]]


async def call_llm_with_retry(
    llm: LLMProvider,
    metrics: PerformanceMetrics,
    chat_id: str,
    messages: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam] | None,
    stream_callback: StreamCallback | None,
    use_streaming: bool,
    iteration: int,
    max_retries: int,
    initial_delay: float,
    retryable_codes: frozenset[ErrorCode],
) -> ChatCompletion | None:
    """Call the LLM with retry for transient errors.

    Returns the LLM completion on success, or ``None`` when the circuit
    breaker is open (caller should return the unavailable message).
    Non-transient and exhausted-transient errors are re-raised.
    """
    from src.utils.retry import calculate_delay_with_jitter

    delay = initial_delay

    for attempt in range(max_retries + 1):
        try:
            if use_streaming:
                return await llm.chat_stream(
                    messages,
                    tools=tools,
                    on_chunk=stream_callback,
                    chat_id=chat_id,
                )
            return await llm.chat(messages, tools=tools, chat_id=chat_id)
        except LLMError as exc:
            if exc.error_code == ErrorCode.LLM_CIRCUIT_BREAKER_OPEN:
                log.warning("Circuit breaker open — returning unavailable message")
                metrics.track_react_iterations(iteration + 1)
                metrics.track_conversation_depth(chat_id, iteration + 1)
                return None
            if exc.error_code not in retryable_codes:
                raise
            if attempt >= max_retries:
                log.warning(
                    "LLM retry exhausted after %d attempts for chat %s: %s",
                    max_retries + 1,
                    chat_id,
                    exc.error_code,
                )
                raise
            actual_delay = calculate_delay_with_jitter(delay)
            log.info(
                "Transient LLM error (%s), retrying attempt %d/%d after %.2fs for chat %s",
                exc.error_code,
                attempt + 1,
                max_retries,
                actual_delay,
                chat_id,
            )
            await asyncio.sleep(actual_delay)
            delay *= 2

    raise RuntimeError("Unexpected state in call_llm_with_retry")  # pragma: no cover


async def react_loop(
    llm: LLMProvider,
    metrics: PerformanceMetrics,
    tool_executor: ToolExecutor,
    chat_id: str,
    messages: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam] | None,
    workspace_dir: Path,
    max_tool_iterations: int,
    stream_response: bool,
    max_retries: int,
    initial_delay: float,
    retryable_codes: frozenset[ErrorCode],
    stream_callback: StreamCallback | None = None,
    channel: BaseChannel | None = None,
) -> tuple[str, list[ToolLogEntry], list[dict]]:
    """Run the ReAct loop and return response text, tool log, and buffered messages.

    Args:
        llm: LLM provider for chat completions.
        metrics: Performance metrics collector.
        tool_executor: Executes tool calls with rate limiting and error handling.
        chat_id: Chat identifier for logging.
        messages: Conversation history for LLM context.
        tools: Available tool definitions.
        workspace_dir: Workspace directory for skill execution.
        max_tool_iterations: Maximum number of ReAct iterations.
        stream_response: Whether to use streaming LLM responses.
        max_retries: Max retry attempts for transient LLM errors.
        initial_delay: Initial delay in seconds for retry backoff.
        retryable_codes: LLM error codes that are transient and worth retrying.
        stream_callback: Optional callback to stream tool executions in real-time.
        channel: Optional channel for media-sending callback injection.

    Returns:
        Tuple of (response_text, tool_log, buffered_persist).
    """
    tool_log: list[ToolLogEntry] = []
    buffered_persist: list[dict] = []

    for iteration in range(max_tool_iterations):
        log.debug(
            "ReAct loop iteration %d/%d, tool_calls_so_far=%d for chat %s",
            iteration + 1,
            max_tool_iterations,
            len(tool_log),
            chat_id,
            extra={
                "chat_id": chat_id,
                "correlation_id": get_correlation_id(),
                "react_iteration": iteration + 1,
                "react_max_iterations": max_tool_iterations,
                "react_tool_count": len(tool_log),
            },
        )

        llm_start = time.perf_counter()

        completion = await call_llm_with_retry(
            llm, metrics, chat_id, messages, tools, stream_callback,
            stream_response, iteration, max_retries, initial_delay, retryable_codes,
        )
        if completion is None:
            return (
                "⚠️ Service temporarily unavailable. "
                "The AI provider is experiencing issues. Please try again in a minute.",
                tool_log,
                buffered_persist,
            )
        llm_latency = time.perf_counter() - llm_start
        metrics.track_llm_latency(llm_latency)

        choice = completion.choices[0]
        finish = choice.finish_reason

        if finish == "tool_calls" or choice.message.tool_calls:
            iteration_log, iteration_buffered = await process_tool_calls(
                tool_executor, choice, messages, chat_id, workspace_dir,
                stream_callback, channel,
            )
            tool_log.extend(iteration_log)
            buffered_persist.extend(iteration_buffered)
        else:
            content = choice.message.content
            if not content or not content.strip():
                metrics.track_react_iterations(iteration + 1)
                metrics.track_conversation_depth(chat_id, iteration + 1)
                return (
                    "(The assistant generated an empty response. "
                    "Please try rephrasing your request.)",
                    tool_log,
                    buffered_persist,
                )
            metrics.track_react_iterations(iteration + 1)
            metrics.track_conversation_depth(chat_id, iteration + 1)
            return content, tool_log, buffered_persist

    log.warning(
        "Reached max tool iterations (%d) for chat %s",
        max_tool_iterations,
        chat_id,
        extra={"chat_id": chat_id, "max_iterations": max_tool_iterations},
    )
    metrics.track_react_iterations(max_tool_iterations)
    metrics.track_conversation_depth(chat_id, max_tool_iterations)
    return (
        format_max_iterations_message(max_tool_iterations, tool_log),
        tool_log,
        buffered_persist,
    )


def format_max_iterations_message(iterations: int, tool_log: list[ToolLogEntry]) -> str:
    """Build an informative message when the ReAct loop hits the iteration cap."""
    tool_summary = ""
    if tool_log:
        tool_names = [entry.name for entry in tool_log]
        unique_tools = dict.fromkeys(tool_names)
        tool_summary = (
            f"\n\n🔧 Tools used ({len(tool_log)} calls): {', '.join(unique_tools)}"
        )
    return (
        f"⚠️ Reached maximum tool iterations ({iterations}). "
        f"The task may be too complex for a single request. "
        f"Try breaking it into smaller steps.{tool_summary}"
    )


async def process_tool_calls(
    tool_executor: ToolExecutor,
    choice: Choice,
    messages: list[ChatCompletionMessageParam],
    chat_id: str,
    workspace_dir: Path,
    stream_callback: StreamCallback | None = None,
    channel: BaseChannel | None = None,
) -> tuple[list[ToolLogEntry], list[dict]]:
    """Process tool calls from an LLM response and append results to messages.

    Executes all requested tool calls in parallel via ``asyncio.TaskGroup``,
    then appends results to *messages* in the original call order.

    Returns:
        Tuple of (tool_log, buffered_persist).
    """
    tool_log: list[ToolLogEntry] = []
    buffered_persist: list[dict] = []

    assistant_msg = serialize_tool_call_message(choice.message)
    messages.append(assistant_msg)
    buffered_persist.append(
        {"role": "assistant", "content": assistant_msg.get("content") or ""}
    )

    send_media = None
    if channel is not None:
        async def _send_media(kind: str, path: Path, caption: str = "") -> None:
            """Route media to the appropriate channel send method."""
            try:
                if kind == "audio":
                    await channel.send_audio(chat_id, path, ptt=True)
                elif kind == "document":
                    await channel.send_document(chat_id, path, caption=caption)
                else:
                    log.warning("Unknown media kind: %s", kind)
            except Exception as exc:
                log.error(
                    "send_media callback failed: %s",
                    exc,
                    extra={"chat_id": chat_id, "correlation_id": get_correlation_id()},
                )

        send_media = _send_media

    tool_calls = choice.message.tool_calls or []
    if not tool_calls:
        return tool_log, buffered_persist

    function_calls = [tc for tc in tool_calls if tc.type == "function"]

    rejected_calls: list = []
    if len(function_calls) > MAX_TOOL_CALLS_PER_TURN:
        rejected_calls = function_calls[MAX_TOOL_CALLS_PER_TURN:]
        function_calls = function_calls[:MAX_TOOL_CALLS_PER_TURN]
        log.warning(
            "Tool-call limit reached in chat %s: %d requested, %d max — "
            "%d calls rejected",
            chat_id,
            len(function_calls) + len(rejected_calls),
            MAX_TOOL_CALLS_PER_TURN,
            len(rejected_calls),
            extra={"chat_id": chat_id},
        )

    results: list[tuple[str, str, ToolLogEntry]] = []
    try:
        async with asyncio.TaskGroup() as tg:
            tasks = [
                tg.create_task(
                    execute_tool_call(tool_executor, tc, chat_id, workspace_dir, send_media)
                )
                for tc in function_calls
            ]
        results = [t.result() for t in tasks]
    except BaseException as exc:
        log.warning(
            "TaskGroup interrupted in chat %s: %s — salvaging %d partial results",
            chat_id,
            type(exc).__name__,
            sum(1 for t in tasks if t.done() and not t.cancelled()),
            extra={"chat_id": chat_id},
        )
        for t in tasks:
            if t.done() and not t.cancelled():
                try:
                    results.append(t.result())
                except BaseException:
                    pass

    for tc_id, content, tool_entry in results:
        tool_msg: ChatCompletionToolMessageParam = {
            "role": "tool",
            "tool_call_id": tc_id,
            "content": content,
        }
        messages.append(tool_msg)
        tool_log.append(tool_entry)
        if len(content) > MAX_TOOL_RESULT_PERSIST_LENGTH:
            persist_content = (
                content[:MAX_TOOL_RESULT_PERSIST_LENGTH]
                + f"\n[truncated, full length: {len(content)}]"
            )
        else:
            persist_content = content
        buffered_persist.append(
            {"role": "tool", "content": persist_content, "name": tool_entry.name}
        )
        if stream_callback:
            formatted = format_single_tool_execution(tool_entry)
            await stream_callback(formatted)

    for tc in rejected_calls:
        tc_id = tc.id
        tool_name = tc.function.name if tc.function else "unknown"
        rejection_msg = (
            f"⚠️ Tool call rejected: per-turn limit of "
            f"{MAX_TOOL_CALLS_PER_TURN} reached. "
            f"Prioritise remaining tasks and retry in the next turn."
        )
        tool_msg: ChatCompletionToolMessageParam = {
            "role": "tool",
            "tool_call_id": tc_id,
            "content": rejection_msg,
        }
        messages.append(tool_msg)
        buffered_persist.append(
            {"role": "tool", "content": rejection_msg, "name": tool_name}
        )

    return tool_log, buffered_persist


async def execute_tool_call(
    tool_executor: ToolExecutor,
    tool_call: ChatCompletionMessageFunctionToolCall,
    chat_id: str,
    workspace_dir: Path,
    send_media: SendMediaCallback | None,
) -> tuple[str, str, ToolLogEntry]:
    """Execute a single tool call, returning result data for message assembly.

    Returns:
        Tuple of ``(tool_call_id, result_content, tool_log_entry)``.
        Never raises — returns error content on any failure.
    """
    tc_id = tool_call.id
    try:
        if not tool_call.function or not tool_call.function.name:
            raise AttributeError("tool_call.function or name is missing")

        ws_resolved = workspace_dir.resolve()
        root_resolved = Path(WORKSPACE_DIR).resolve()
        if not ws_resolved.is_relative_to(root_resolved):
            log.error(
                "Path traversal detected: workspace_dir %s is outside %s for chat %s",
                ws_resolved,
                root_resolved,
                chat_id,
                extra={"chat_id": chat_id},
            )
            await get_event_bus().emit(Event(
                name="error_occurred",
                data={
                    "error_type": "path_traversal",
                    "chat_id": chat_id,
                    "workspace_dir": str(ws_resolved),
                    "root_dir": str(root_resolved),
                    "tool_name": tool_call.function.name,
                },
                source="react_loop.execute_tool_call",
                correlation_id=get_correlation_id(),
            ))
            return (
                tc_id,
                "⚠️ Workspace path validation failed. This incident has been logged.",
                ToolLogEntry(
                    name=tool_call.function.name,
                    args={},
                    result="Path traversal blocked.",
                ),
            )

        result = await tool_executor.execute(
            chat_id=chat_id,
            tool_call=tool_call,
            workspace_dir=workspace_dir,
            send_media=send_media,
        )
        try:
            args = json.loads(tool_call.function.arguments or "{}")
        except JSONDecodeError:
            args = {}
        return (
            tc_id,
            result,
            ToolLogEntry(name=tool_call.function.name, args=args, result=result),
        )

    except (AttributeError, TypeError) as exc:
        log.error(
            "Malformed tool_call structure in chat %s: %s",
            chat_id,
            exc,
            extra={"chat_id": chat_id, "correlation_id": get_correlation_id()},
            exc_info=True,
        )
        return (
            tc_id,
            "⚠️ Malformed tool call: function name or "
            "arguments were missing or invalid. "
            "Please retry with properly formatted tool calls.",
            ToolLogEntry(
                name=getattr(tool_call.function, "name", "unknown"),
                args={},
                result="Malformed tool call — skipped.",
            ),
        )
