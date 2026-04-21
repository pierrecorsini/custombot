"""
src/core/tool_formatter.py — Tool execution log formatting for display and streaming.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

MAX_RESULT_LEN = 500


@dataclass(slots=True, frozen=True)
class ToolLogEntry:
    """Typed record of a single tool execution within the ReAct loop.

    Replaces ``dict[str, Any]`` entries in the tool log so that every
    field is known at type-check time and callers never need
    ``entry.get("name", "unknown")`` defensive access.
    """

    name: str
    args: dict[str, Any]
    result: str


def format_response_with_tool_log(
    response_text: str,
    tool_log: list[ToolLogEntry],
) -> str:
    """
    Format response text with tool execution log for display.

    Args:
        response_text: The LLM's final response text.
        tool_log: List of :class:`ToolLogEntry` records.

    Returns:
        Formatted response with tool execution details appended.
    """
    if not tool_log:
        return response_text

    lines = ["\n\n---\n## 🔧 Tool Executions"]

    for i, entry in enumerate(tool_log, 1):
        args_str = _format_args(entry.args)
        result = _truncate_result(entry.result)

        lines.append(f"\n**{i}. `{entry.name}{args_str}`**")
        lines.append(f"```\n{result}\n```")

    return response_text + "\n".join(lines)


def format_single_tool_execution(entry: ToolLogEntry) -> str:
    """
    Format a single tool execution for real-time streaming.

    Args:
        entry: :class:`ToolLogEntry` record.

    Returns:
        Formatted tool execution message.
    """
    args_str = _format_args(entry.args)
    result = _truncate_result(entry.result)

    return f"🔧 *Tool:* `{entry.name}{args_str}`\n```\n{result}\n```"


def _format_args(args: dict[str, Any]) -> str:
    """Format args dict as (key='value', ...) string."""
    if not args:
        return "()"
    return "(" + ", ".join(f"{k}={v!r}" for k, v in args.items()) + ")"


def _truncate_result(result: str) -> str:
    """Truncate long results for display."""
    if len(result) > MAX_RESULT_LEN:
        return result[:MAX_RESULT_LEN] + "..."
    return result
