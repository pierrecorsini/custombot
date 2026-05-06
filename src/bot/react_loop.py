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
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable


from src.constants import MAX_TOOL_CALLS_PER_TURN, MAX_TOOL_RESULT_PERSIST_LENGTH, WORKSPACE_DIR
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import Event, get_event_bus
from src.core.serialization import serialize_tool_call_message
from src.core.tool_formatter import ToolLogEntry, format_single_tool_execution
from src.exceptions import ErrorCode, LLMError
from src.logging import get_correlation_id
from src.monitoring.tracing import (
    llm_call_span,
    react_loop_span,
    record_exception_safe,
    set_correlation_id_on_span,
    skill_execution_span,
    tool_calls_span,
)
from src.utils import JSONDecodeError
from src.utils.timing import elapsed as _elapsed, set_timer_start as _set_timer_start

if TYPE_CHECKING:
    from openai.types.chat.chat_completion import Choice
    from src.core.tool_executor import ToolExecutor
    from openai.types.chat.chat_completion_message_function_tool_call import (
        ChatCompletionMessageFunctionToolCall,
    )
    from src.channels.base import SendMediaCallback
    from openai.types.chat import (
        ChatCompletion,
        ChatCompletionMessageParam,
        ChatCompletionToolMessageParam,
        ChatCompletionToolParam,
    )
    from src.monitoring import PerformanceMetrics
    from src.channels.base import BaseChannel
    from src.llm import LLMProvider
    from src.monitoring.tracing import Span

log = logging.getLogger(__name__)

# Type alias for streaming tool execution updates
StreamCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True, frozen=True)
class ReactIterationContext:
    """Invariant parameters shared across all ReAct loop iterations.

    Replaces the former 13-invariant-parameter threading into
    ``_react_iteration()``, mirroring ``ShutdownContext`` from
    ``src/lifecycle.py`` and ``BotDeps`` from ``src/bot/_bot.py``.

    Constructed once in ``react_loop()`` and passed to each iteration
    so that per-iteration state (``iteration``, ``messages``, etc.) is
    the only thing that changes between calls.
    """

    # Required — always present during a ReAct loop
    llm: LLMProvider
    metrics: PerformanceMetrics
    tool_executor: ToolExecutor
    chat_id: str
    tools: list[ChatCompletionToolParam] | None
    workspace_dir: Path
    stream_response: bool
    max_tool_iterations: int
    max_retries: int
    initial_delay: float
    retryable_codes: frozenset[ErrorCode]

    # Optional — may be ``None`` depending on config
    stream_callback: StreamCallback | None = None
    channel: BaseChannel | None = None
    # Monotonic deadline (``time.monotonic()``) for wall-clock timeout.
    # ``None`` means no deadline (disabled).
    deadline: float | None = None


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
        tool_count = len(tools) if tools else 0
        with llm_call_span(
            chat_id=chat_id,
            iteration=iteration,
            use_streaming=use_streaming,
            tool_count=tool_count,
        ) as span:
            set_correlation_id_on_span(span, get_correlation_id())
            span.set_attribute("custombot.llm.attempt", attempt + 1)
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
                record_exception_safe(span, exc)
                span.set_attribute("custombot.llm.error_code", exc.error_code.value)
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
    react_loop_timeout: float = 0.0,
) -> tuple[str, list[ToolLogEntry], list[dict[str, Any]]]:
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
        react_loop_timeout: Wall-clock timeout in seconds for the full loop.
            Checked between iterations; 0 disables the deadline.

    Returns:
        Tuple of (response_text, tool_log, buffered_persist).
    """
    tool_log: list[ToolLogEntry] = []
    buffered_persist: list[dict[str, Any]] = []

    deadline: float | None = None
    if react_loop_timeout and react_loop_timeout > 0:
        deadline = time.monotonic() + react_loop_timeout

    ctx = ReactIterationContext(
        llm=llm,
        metrics=metrics,
        tool_executor=tool_executor,
        chat_id=chat_id,
        tools=tools,
        workspace_dir=workspace_dir,
        stream_response=stream_response,
        max_tool_iterations=max_tool_iterations,
        max_retries=max_retries,
        initial_delay=initial_delay,
        retryable_codes=retryable_codes,
        stream_callback=stream_callback,
        channel=channel,
        deadline=deadline,
    )

    for iteration in range(max_tool_iterations):
        # ── Wall-clock deadline check ────────────────────────────────────
        if ctx.deadline is not None and time.monotonic() >= ctx.deadline:
            elapsed = time.monotonic() - (ctx.deadline - react_loop_timeout)
            log.warning(
                "ReAct loop wall-clock timeout exceeded (%.1fs / %.1fs) at "
                "iteration %d/%d for chat %s — terminating gracefully",
                elapsed,
                react_loop_timeout,
                iteration,
                max_tool_iterations,
                chat_id,
                extra={
                    "chat_id": chat_id,
                    "iteration": iteration,
                    "elapsed_seconds": round(elapsed, 2),
                    "timeout_seconds": react_loop_timeout,
                },
            )
            ctx.metrics.track_react_iterations(iteration)
            ctx.metrics.track_conversation_depth(chat_id, iteration)
            return (
                format_timeout_message(react_loop_timeout, iteration, tool_log),
                tool_log,
                buffered_persist,
            )

        with react_loop_span(chat_id, iteration + 1, max_tool_iterations) as span:
            result = await _react_iteration(
                ctx,
                iteration,
                messages,
                tool_log,
                buffered_persist,
                span,
            )
            if result is not None:
                return result

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


async def _react_iteration(
    ctx: ReactIterationContext,
    iteration: int,
    messages: list[ChatCompletionMessageParam],
    tool_log: list[ToolLogEntry],
    buffered_persist: list[dict[str, Any]],
    span: Span,
) -> tuple[str, list[ToolLogEntry], list[dict[str, Any]]] | None:
    """Execute a single ReAct loop iteration.

    Calls the LLM, then either:

    * Returns ``None`` to signal that tool calls were processed and the
      outer loop should continue to the next iteration.
    * Returns a ``(text, tool_log, buffered_persist)`` tuple to terminate
      the loop (final text response, circuit-breaker fallback, or empty
      response).

    All OpenTelemetry span helpers (``llm_call_span``, ``tool_calls_span``,
    ``skill_execution_span``) are exercised via the helpers called from
    here, and per-iteration attributes are recorded on *span*.
    """
    _set_timer_start()

    # ── LLM call (wrapped in llm_call_span by call_llm_with_retry) ──────
    response = await call_llm_with_retry(
        llm=ctx.llm,
        metrics=ctx.metrics,
        chat_id=ctx.chat_id,
        messages=messages,
        tools=ctx.tools,
        stream_callback=ctx.stream_callback,
        use_streaming=ctx.stream_response,
        iteration=iteration,
        max_retries=ctx.max_retries,
        initial_delay=ctx.initial_delay,
        retryable_codes=ctx.retryable_codes,
    )

    # Circuit breaker is open — LLM unavailable
    if response is None:
        ctx.metrics.track_react_iterations(iteration + 1)
        ctx.metrics.track_conversation_depth(ctx.chat_id, iteration + 1)
        span.set_attribute("custombot.react.circuit_breaker_open", True)
        return (
            "⚠️ Service temporarily unavailable. Please try again shortly.",
            tool_log,
            buffered_persist,
        )

    choice = response.choices[0]
    finish_reason = choice.finish_reason
    content = choice.message.content
    has_tool_calls = choice.message.tool_calls is not None and len(choice.message.tool_calls) > 0

    # Record LLM latency and response metadata on the iteration span
    llm_latency = _elapsed()
    ctx.metrics.track_llm_latency(llm_latency)
    span.set_attribute("custombot.react.finish_reason", finish_reason or "unknown")
    span.set_attribute("custombot.llm.latency_ms", round(llm_latency * 1000, 2))

    # ── Tool calls present — process and continue the loop ───────────────
    if has_tool_calls or finish_reason == "tool_calls":
        iteration_tool_log, iteration_buffered = await process_tool_calls(
            tool_executor=ctx.tool_executor,
            choice=choice,
            messages=messages,
            chat_id=ctx.chat_id,
            workspace_dir=ctx.workspace_dir,
            stream_callback=ctx.stream_callback,
            channel=ctx.channel,
        )
        tool_log.extend(iteration_tool_log)
        buffered_persist.extend(iteration_buffered)
        span.set_attribute("custombot.react.tools_executed", len(iteration_tool_log))
        # Return None to continue the outer loop
        return None

    # ── Terminal response (stop / length / content_filter) ───────────────
    ctx.metrics.track_react_iterations(iteration + 1)
    ctx.metrics.track_conversation_depth(ctx.chat_id, iteration + 1)

    if content and content.strip():
        span.set_attribute("custombot.react.response_length", len(content))
        if finish_reason == "length":
            span.set_attribute("custombot.react.truncated", True)
            return (
                content + "\n\n⚠️ Response truncated due to length limit. "
                "Try asking a more specific question.",
                tool_log,
                buffered_persist,
            )
        return (content, tool_log, buffered_persist)

    # Truncated response — LLM hit token limit before generating content
    if finish_reason == "length":
        span.set_attribute("custombot.react.truncated", True)
        return (
            "⚠️ Response truncated due to length limit. Try asking a more specific question.",
            tool_log,
            buffered_persist,
        )

    # Empty / whitespace-only response
    span.set_attribute("custombot.react.empty_response", True)
    return (
        "(The assistant generated an empty response. Please try rephrasing your message.)",
        tool_log,
        buffered_persist,
    )


def format_max_iterations_message(iterations: int, tool_log: list[ToolLogEntry]) -> str:
    """Build an informative message when the ReAct loop hits the iteration cap."""
    tool_summary = ""
    if tool_log:
        tool_names = [entry.name for entry in tool_log]
        unique_tools = dict.fromkeys(tool_names)
        tool_summary = f"\n\n🔧 Tools used ({len(tool_log)} calls): {', '.join(unique_tools)}"
    return (
        f"⚠️ Reached maximum tool iterations ({iterations}). "
        f"The task may be too complex for a single request. "
        f"Try breaking it into smaller steps.{tool_summary}"
    )


def format_timeout_message(
    timeout_seconds: float,
    iteration: int,
    tool_log: list[ToolLogEntry],
) -> str:
    """Build an informative message when the ReAct loop exceeds the wall-clock timeout."""
    tool_summary = ""
    if tool_log:
        tool_names = [entry.name for entry in tool_log]
        unique_tools = dict.fromkeys(tool_names)
        tool_summary = f"\n\n🔧 Tools used ({len(tool_log)} calls): {', '.join(unique_tools)}"
    return (
        f"⚠️ Processing timed out after {timeout_seconds:.0f}s "
        f"(completed {iteration} iteration{'s' if iteration != 1 else ''}). "
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
) -> tuple[list[ToolLogEntry], list[dict[str, Any]]]:
    """Process tool calls from an LLM response and append results to messages.

    Executes all requested tool calls in parallel via ``asyncio.TaskGroup``,
    then appends results to *messages* in the original call order.

    Returns:
        Tuple of (tool_log, buffered_persist).
    """
    tool_log: list[ToolLogEntry] = []
    buffered_persist: list[dict[str, Any]] = []

    assistant_msg = serialize_tool_call_message(choice.message)
    messages.append(assistant_msg)
    buffered_persist.append({"role": "assistant", "content": assistant_msg.get("content") or ""})

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

    with tool_calls_span(chat_id=chat_id, call_count=len(function_calls)):
        rejected_calls: list[ChatCompletionMessageFunctionToolCall] = []
        if len(function_calls) > MAX_TOOL_CALLS_PER_TURN:
            rejected_calls = function_calls[MAX_TOOL_CALLS_PER_TURN:]
            function_calls = function_calls[:MAX_TOOL_CALLS_PER_TURN]
            log.warning(
                "Tool-call limit reached in chat %s: %d requested, %d max — %d calls rejected",
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
            rejection_tool_msg: ChatCompletionToolMessageParam = {
                "role": "tool",
                "tool_call_id": tc_id,
                "content": rejection_msg,
            }
            messages.append(rejection_tool_msg)
            buffered_persist.append({"role": "tool", "content": rejection_msg, "name": tool_name})

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
    skill_name = getattr(tool_call.function, "name", "unknown")
    raw_args = tool_call.function.arguments or "{}"
    args_size = len(raw_args.encode("utf-8"))

    with skill_execution_span(
        skill_name=skill_name,
        chat_id=chat_id,
        args_size_bytes=args_size,
    ) as span:
        set_correlation_id_on_span(span, get_correlation_id())
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
                await get_event_bus().emit(
                    Event(
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
                    )
                )
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
            span.set_attribute("custombot.skill.result_length", len(result))
            return (
                tc_id,
                result,
                ToolLogEntry(name=tool_call.function.name, args=raw_args, result=result),
            )

        except (AttributeError, TypeError) as exc:
            record_exception_safe(span, exc)
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
