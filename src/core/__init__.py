"""
src/core/__init__.py — Core business logic module.

This module contains the essential business logic components extracted
from bot.py for better maintainability and single responsibility.
"""

from src.core.tool_executor import (
    ToolExecutor,
    format_skill_error,
    get_error_suggestion,
)
from src.core.context_builder import build_context, db_rows_to_messages
from src.core.tool_formatter import (
    format_response_with_tool_log,
    format_single_tool_execution,
)
from src.core.instruction_loader import InstructionLoader
from src.core.project_context import ProjectContextLoader

__all__ = [
    "ToolExecutor",
    "format_skill_error",
    "get_error_suggestion",
    "build_context",
    "db_rows_to_messages",
    "format_response_with_tool_log",
    "format_single_tool_execution",
    "InstructionLoader",
    "ProjectContextLoader",
]
