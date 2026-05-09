"""
src/core/__init__.py — Core business logic module.

This module contains the essential business logic components extracted
from bot.py for better maintainability and single responsibility.
"""

from src.core.context_builder import ChatMessage, HistoryBundle, build_context, db_rows_to_messages
from src.core.event_bus import (
    EVENT_ERROR_OCCURRED,
    EVENT_MESSAGE_RECEIVED,
    EVENT_RESPONSE_SENT,
    EVENT_SHUTDOWN_STARTED,
    EVENT_SKILL_EXECUTED,
    KNOWN_EVENTS,
    Event,
    EventBus,
    EventHandler,
    emit_error_event,
    get_event_bus,
    reset_event_bus,
)
from src.core.instruction_loader import InstructionLoader
from src.core.project_context import ProjectContextLoader
from src.core.serialization import serialize_tool_call_message
from src.core.skill_breaker_registry import SkillBreakerRegistry
from src.core.tool_executor import (
    ToolExecutor,
    format_skill_error,
    get_error_suggestion,
)
from src.core.tool_formatter import (
    ToolLogEntry,
    format_response_with_tool_log,
    format_single_tool_execution,
)

__all__ = [
    "ChatMessage",
    "HistoryBundle",
    "KNOWN_EVENTS",
    "Event",
    "EventBus",
    "EventHandler",
    "SkillBreakerRegistry",
    "ToolExecutor",
    "ToolLogEntry",
    "EVENT_ERROR_OCCURRED",
    "EVENT_MESSAGE_RECEIVED",
    "EVENT_RESPONSE_SENT",
    "EVENT_SHUTDOWN_STARTED",
    "EVENT_SKILL_EXECUTED",
    "emit_error_event",
    "format_skill_error",
    "get_error_suggestion",
    "build_context",
    "db_rows_to_messages",
    "format_response_with_tool_log",
    "format_single_tool_execution",
    "get_event_bus",
    "InstructionLoader",
    "ProjectContextLoader",
    "reset_event_bus",
    "serialize_tool_call_message",
]
