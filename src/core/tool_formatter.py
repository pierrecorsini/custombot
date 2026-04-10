"""
src/core/tool_formatter.py — Tool execution log formatting for display and streaming.
"""

from __future__ import annotations

from typing import Any

MAX_RESULT_LEN = 500


def format_response_with_tool_log(
    response_text: str,
    tool_log: list[dict[str, Any]],
) -> str:
    """
    Format response text with tool execution log for display.

    Args:
        response_text: The LLM's final response text.
        tool_log: List of tool execution dicts with 'name', 'args', 'result'.

    Returns:
        Formatted response with tool execution details appended.
    """
    if not tool_log:
        return response_text

    lines = ["\n\n---\n## 🔧 Tool Executions"]

    for i, entry in enumerate(tool_log, 1):
        name = entry.get("name", "unknown")
        args = entry.get("args", {})
        result = entry.get("result", "")

        args_str = _format_args(args)
        result = _truncate_result(result)

        lines.append(f"\n**{i}. `{name}{args_str}`**")
        lines.append(f"```\n{result}\n```")

    return response_text + "\n".join(lines)


def format_single_tool_execution(entry: dict[str, Any]) -> str:
    """
    Format a single tool execution for real-time streaming.

    Args:
        entry: Dict with 'name', 'args', and 'result' keys.

    Returns:
        Formatted tool execution message.
    """
    name = entry.get("name", "unknown")
    args = entry.get("args", {})
    result = entry.get("result", "")

    args_str = _format_args(args)
    result = _truncate_result(result)

    return f"🔧 *Tool:* `{name}{args_str}`\n```\n{result}\n```"


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
