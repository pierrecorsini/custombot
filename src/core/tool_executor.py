"""
src/core/tool_executor.py — Tool execution logic extracted from bot.py.

Handles skill execution with:
- Rate limiting
- Timeout management
- Error formatting
- Metrics tracking
- Media callback injection for audio/PDF skills
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from openai.types.chat.chat_completion_message_function_tool_call import ChatCompletionMessageFunctionToolCall

from src.constants import DEFAULT_SKILL_TIMEOUT, SLOW_SKILL_THRESHOLD_SECONDS
from src.core.event_bus import Event, get_event_bus
from src.exceptions import SkillError, get_user_friendly_message
from src.logging import get_correlation_id
from src.security.audit import SkillAuditLogger
from src.utils import JSONDecodeError
from src.utils.timing import skill_timer

MAX_ARGS_DEPTH = 10
MAX_ARGS_BYTES = 1_048_576  # 1 MiB

if TYPE_CHECKING:
    from src.monitoring import PerformanceMetrics
    from src.rate_limiter import RateLimiter
    from src.skills import SkillRegistry

log = logging.getLogger(__name__)

# Type alias matching channels.base.SendMediaCallback
SendMediaCallback = Callable[[str, Path, str], Awaitable[None]]


class ToolExecutor:
    """Executes skills with rate limiting, timeouts, and error handling."""

    def __init__(
        self,
        skills_registry: "SkillRegistry",
        rate_limiter: "RateLimiter | None" = None,
        metrics: "PerformanceMetrics | None" = None,
        on_skill_executed: Optional[Callable[[], None]] = None,
        audit_log_dir: Optional[str | Path] = None,
    ) -> None:
        self._skills = skills_registry
        self._rate_limiter = rate_limiter
        self._metrics = metrics
        self._on_skill_executed = on_skill_executed
        self._audit_log_dir = Path(audit_log_dir) if audit_log_dir else None
        self._audit_logger: SkillAuditLogger | None = None

    def close(self) -> None:
        """Flush and close the audit logger. Safe to call multiple times."""
        if self._audit_logger is not None:
            self._audit_logger.close()
            self._audit_logger = None
            log.debug("ToolExecutor audit logger closed")

    def _audit(
        self,
        chat_id: str,
        skill_name: str,
        raw_args: str,
        allowed: bool,
        result_summary: str,
    ) -> None:
        """Record a skill-execution audit entry if the logger is configured."""
        if self._audit_logger is None:
            if self._audit_log_dir is None:
                return
            self._audit_logger = SkillAuditLogger(self._audit_log_dir)
            self._audit_log_dir = None  # release the path reference
        self._audit_logger.log(
            chat_id=chat_id,
            skill_name=skill_name,
            args_hash=SkillAuditLogger.hash_args(raw_args),
            allowed=allowed,
            result_summary=result_summary,
        )

    async def execute(
        self,
        chat_id: str,
        tool_call: ChatCompletionMessageFunctionToolCall,
        workspace_dir: Path,
        send_media: Optional[SendMediaCallback] = None,
    ) -> str:
        """
        Execute a tool call with full error handling and rate limiting.

        Args:
            chat_id: Chat identifier for logging.
            tool_call: The tool call object from LLM response.
            workspace_dir: Workspace directory for skill execution.
            send_media: Optional async callback for media skills to send
                audio/documents directly to the channel.

        Returns:
            Tool result as string (or formatted error message).
        """
        # Extract and validate skill name
        try:
            name = tool_call.function.name
        except AttributeError:
            log.error(
                "Malformed tool call: missing 'function' or 'name' attribute",
                extra={"chat_id": chat_id},
            )
            self._audit(chat_id, "unknown", "{}", False, "malformed_tool_call")
            return format_skill_error(
                skill_name="unknown",
                error_type="MalformedToolCall",
                user_message="The tool call was malformed.",
            )

        # Reject oversized payloads before parsing
        raw_args = tool_call.function.arguments or "{}"
        if len(raw_args) > MAX_ARGS_BYTES:
            log.warning(
                "Skill %r arguments exceeded max size %d bytes (got %d)",
                name,
                MAX_ARGS_BYTES,
                len(raw_args),
                extra={"chat_id": chat_id, "skill": name},
            )
            self._audit(chat_id, name, raw_args, False, "args_oversized")
            if self._metrics is not None:
                self._metrics.track_skill_args_oversized(name, len(raw_args))
            return format_skill_error(
                skill_name=name,
                error_type="ArgumentError",
                user_message="The arguments are too large.",
            )

        # Parse arguments
        try:
            args = json.loads(raw_args)
        except JSONDecodeError as exc:
            log.error(
                "Skill %r argument parse failed: %s",
                name,
                exc,
                exc_info=True,
                extra={"chat_id": chat_id, "skill": name},
            )
            self._audit(chat_id, name, raw_args, False, "args_parse_error")
            return format_skill_error(
                skill_name=name,
                error_type="ArgumentError",
                user_message="I couldn't understand the arguments for this tool.",
            )

        if _measured_depth(args) > MAX_ARGS_DEPTH:
            log.warning(
                "Skill %r arguments exceeded max nesting depth %d",
                name,
                MAX_ARGS_DEPTH,
                extra={"chat_id": chat_id, "skill": name},
            )
            self._audit(chat_id, name, raw_args, False, "args_too_deep")
            return format_skill_error(
                skill_name=name,
                error_type="ArgumentError",
                user_message="The arguments are too deeply nested.",
            )

        # Get skill
        skill = self._skills.get(name)
        if skill is None:
            log.error(
                "Unknown skill requested: %s",
                name,
                extra={"chat_id": chat_id, "skill": name},
            )
            self._audit(chat_id, name, raw_args, False, "unknown_skill")
            return format_skill_error(
                skill_name=name,
                error_type="UnknownSkill",
                user_message="This tool is not available.",
            )

        # Check rate limits
        if self._rate_limiter:
            rate_result = self._rate_limiter.check_rate_limit(chat_id, name)
            if not rate_result.allowed:
                log.warning(
                    "Rate limit exceeded for skill %r in chat %s",
                    name,
                    chat_id,
                    extra={
                        "chat_id": chat_id,
                        "skill": name,
                        "rate_limit": rate_result.limit_value,
                    },
                )
                self._audit(chat_id, name, raw_args, False, "rate_limited")
                return rate_result.message

        log.info(
            "Executing skill %r in workspace %s",
            name,
            workspace_dir,
            extra={"chat_id": chat_id, "skill": name},
        )

        # Execute with timeout and error handling
        try:
            async with skill_timer(
                skill_name=name,
                chat_id=chat_id,
                slow_threshold=SLOW_SKILL_THRESHOLD_SECONDS,
            ) as timing_result:
                # Build kwargs — inject send_media callback if the skill might use it
                exec_kwargs = dict(args)
                if send_media is not None:
                    exec_kwargs["send_media"] = send_media
                timeout = getattr(skill, "timeout_seconds", DEFAULT_SKILL_TIMEOUT)
                if not isinstance(timeout, (int, float)):
                    timeout = DEFAULT_SKILL_TIMEOUT
                result = await asyncio.wait_for(
                    skill.execute(workspace_dir=workspace_dir, **exec_kwargs),
                    timeout=timeout,
                )
                if self._metrics:
                    self._metrics.track_skill_time(name, timing_result.duration_seconds)
                    self._metrics.track_skill_success(name)
                    self._metrics.track_skill_timeout_ratio(
                        name, timing_result.duration_seconds, timeout
                    )
                log.info(
                    "Skill %r completed",
                    name,
                    extra={
                        "chat_id": chat_id,
                        "skill": name,
                        "duration_ms": round(timing_result.duration_ms, 2),
                        "result_status": "success",
                    },
                )
                self._audit(chat_id, name, raw_args, True, "success")

                # Emit skill_executed event for plugins/subscribers
                try:
                    await get_event_bus().emit(Event(
                        name="skill_executed",
                        data={
                            "skill_name": name,
                            "chat_id": chat_id,
                            "duration_ms": round(timing_result.duration_ms, 2),
                        },
                        source="ToolExecutor",
                    ))
                except Exception:
                    pass  # Event emission must never break skill execution

                return str(result) if result is not None else ""

        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            if self._metrics:
                self._metrics.track_skill_error(name, "TimeoutError")
            log.error(
                "Skill %r timed out",
                name,
                extra={
                    "chat_id": chat_id,
                    "skill": name,
                    "duration_ms": round(timing_result.duration_ms, 2),
                    "result_status": "error",
                    "error_type": "TimeoutError",
                },
            )
            self._audit(chat_id, name, raw_args, True, "error:TimeoutError")
            skill_timeout = getattr(skill, "timeout_seconds", DEFAULT_SKILL_TIMEOUT)
            if not isinstance(skill_timeout, (int, float)):
                skill_timeout = DEFAULT_SKILL_TIMEOUT
            return format_skill_error(
                skill_name=name,
                error_type="TimeoutError",
                user_message=f"The operation took too long (timeout: {skill_timeout}s).",
            )
        except SkillError as exc:
            error_type = exc.details.get("reason", "SkillError")
            user_message = get_user_friendly_message(exc.message, error_type)
            if self._metrics:
                self._metrics.track_skill_error(name, error_type)
            log.error(
                "Skill %r failed",
                name,
                extra={
                    "chat_id": chat_id,
                    "skill": name,
                    "duration_ms": round(timing_result.duration_ms, 2),
                    "result_status": "error",
                    "error_type": error_type,
                },
            )
            return format_skill_error(
                skill_name=name,
                error_type=error_type,
                user_message=user_message,
            )
        except Exception as exc:
            error_type = type(exc).__name__
            if self._metrics:
                self._metrics.track_skill_error(name, error_type)
            log.error(
                "Skill %r failed unexpectedly",
                name,
                extra={
                    "chat_id": chat_id,
                    "skill": name,
                    "duration_ms": round(timing_result.duration_ms, 2),
                    "result_status": "error",
                    "error_type": error_type,
                },
            )
            return format_skill_error(
                skill_name=name,
                error_type=error_type,
                user_message="An unexpected error occurred while executing this tool.",
            )
        finally:
            if self._on_skill_executed:
                self._on_skill_executed()


# ── Argument Depth Validation ────────────────────────────────────


def _measured_depth(obj: Any, _current: int = 0) -> int:
    """Return the maximum nesting depth of a JSON-parsed structure."""
    if isinstance(obj, dict):
        if not obj:
            return _current
        return max(_measured_depth(v, _current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return _current
        return max(_measured_depth(v, _current + 1) for v in obj)
    return _current


# ── Error Formatting Functions ─────────────────────────────────────


def format_skill_error(
    skill_name: str,
    error_type: str,
    user_message: str,
) -> str:
    """Format a user-friendly error message for skill failures."""
    corr_id = get_correlation_id()
    parts = [f"⚠️ {user_message}"]

    suggestion = get_error_suggestion(error_type)
    if suggestion:
        parts.append(f"💡 {suggestion}")

    ref_parts = [f"skill: {skill_name}", f"error: {error_type}"]
    if corr_id:
        ref_parts.append(f"ref: {corr_id}")
    parts.append(f"🔢 {' | '.join(ref_parts)}")

    return "\n".join(parts)


def get_error_suggestion(error_type: str) -> str | None:
    """Get an actionable suggestion based on the error type."""
    suggestions = {
        "TimeoutError": "The operation took too long. Try a simpler request.",
        "PermissionError": "Check if the file or directory exists and is accessible.",
        "FileNotFoundError": "Make sure the file exists in your workspace.",
        "ArgumentError": "Check the command syntax and try again.",
        "UnknownSkill": "Run 'skills list' to see available commands.",
        "ValidationError": "Check your input format and try again.",
        "RateLimitError": "Wait a moment before trying again.",
    }
    return suggestions.get(error_type)
