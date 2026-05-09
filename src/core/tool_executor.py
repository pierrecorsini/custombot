"""
src/core/tool_executor.py — Tool execution logic extracted from bot.py.

Handles skill execution with:
- Per-skill circuit breakers
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
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional


from src.constants import (
    DEFAULT_SKILL_TIMEOUT,
    SKILL_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
    SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
    SLOW_SKILL_THRESHOLD_SECONDS,
)
from src.core.errors import NonCriticalCategory, log_noncritical
from src.core.event_bus import Event, get_event_bus
from src.core.human_approval import ApprovalManager
from src.core.skill_breaker_registry import SkillBreakerRegistry
from src.exceptions import SkillError, get_user_friendly_message
from src.logging import get_correlation_id
from src.security.audit import SkillAuditLogger
from src.utils import JSONDecodeError
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.timing import skill_timer

MAX_ARGS_DEPTH = 10
MAX_ARGS_BYTES = 1_048_576  # 1 MiB
_MAX_TOOL_NAME_LENGTH = 100
_MAX_ERROR_FIELD_LEN = 200
_MAX_ERROR_RESPONSE_LEN = 800

# Control characters, newlines, ANSI escape sequences stripped from tool names
# to prevent log forging and audit trail injection.
_TOOL_NAME_SANITIZE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|[\x00-\x1f\x7f-\x9f]")


def _sanitize_tool_name(name: str) -> str:
    """Strip control characters and truncate tool/skill names for safe logging."""
    cleaned = _TOOL_NAME_SANITIZE_RE.sub("", name)
    if len(cleaned) > _MAX_TOOL_NAME_LENGTH:
        cleaned = cleaned[:_MAX_TOOL_NAME_LENGTH]
    return cleaned or "unknown"


# Mapping of internal error types to user-safe display names.
# Internal Python exception class names (ValueError, OSError, RuntimeError, etc.)
# must never reach end users — they reveal implementation details that could aid
# attackers in crafting targeted exploits.
_SAFE_ERROR_DISPLAY: dict[str, str] = {
    # Domain-specific error categories — safe for user display
    "TimeoutError": "timeout",
    "ArgumentError": "invalid_arguments",
    "UnknownSkill": "unknown_tool",
    "CircuitBreakerOpen": "service_unavailable",
    "MalformedToolCall": "invalid_request",
    "ValidationError": "validation_error",
    "RateLimitError": "rate_limited",
    "SkillError": "skill_error",
    # Python builtins mapped to generic safe labels
    "PermissionError": "permission_denied",
    "FileNotFoundError": "not_found",
    "DiskSpaceError": "storage_error",
}

_UNKNOWN_ERROR_DISPLAY = "internal_error"


def _sanitize_error_type(error_type: str) -> str:
    """Convert an internal error type to a user-safe display name.

    Raw Python exception class names are never shown to end users.
    Unknown types are replaced with a generic ``internal_error`` label.
    """
    return _SAFE_ERROR_DISPLAY.get(error_type, _UNKNOWN_ERROR_DISPLAY)


if TYPE_CHECKING:
    from openai.types.chat.chat_completion_message_function_tool_call import (
        ChatCompletionMessageFunctionToolCall,
    )
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
        approval_timeout_seconds: float = 60.0,
        send_message_fn: Optional[Any] = None,
    ) -> None:
        self._skills = skills_registry
        self._rate_limiter = rate_limiter
        self._metrics = metrics
        self._on_skill_executed = on_skill_executed
        self._audit_logger: SkillAuditLogger | None = (
            SkillAuditLogger(Path(audit_log_dir)) if audit_log_dir else None
        )
        self._skill_breakers = SkillBreakerRegistry(
            failure_threshold=SKILL_CIRCUIT_BREAKER_FAILURE_THRESHOLD,
            cooldown_seconds=SKILL_CIRCUIT_BREAKER_COOLDOWN_SECONDS,
        )
        self._approval_manager = ApprovalManager(timeout_seconds=approval_timeout_seconds)
        self._send_message_fn = send_message_fn

    def get_breaker_states(self) -> dict[str, str]:
        """Return a snapshot of per-skill circuit breaker states.

        Delegates to ``SkillBreakerRegistry.get_breaker_states()``.
        Used by the health endpoint to expose which skills are degraded.
        """
        return self._skill_breakers.get_breaker_states()

    def close(self) -> None:
        """Flush and close the audit logger. Safe to call multiple times."""
        if self._audit_logger is not None:
            self._audit_logger.close()
            log.debug("ToolExecutor audit logger closed")
        self._audit_logger = None

    def _audit(
        self,
        chat_id: str,
        skill_name: str,
        raw_args: str,
        allowed: bool,
        result_summary: str,
    ) -> None:
        """Record a skill-execution audit entry if the logger is configured."""
        logger = self._audit_logger
        if logger is None:
            return
        logger.log(
            chat_id=chat_id,
            skill_name=skill_name,
            args_hash=SkillAuditLogger.hash_args(raw_args),
            allowed=allowed,
            result_summary=result_summary,
        )

    def _get_breaker(self, skill_name: str) -> CircuitBreaker:
        """Return (or lazily create) a CircuitBreaker for *skill_name*."""
        return self._skill_breakers.get_or_create(skill_name)

    @staticmethod
    def _otel_track_tool_error(skill_name: str, error_type: str) -> None:
        """Record a tool error in OTel instruments (best-effort)."""
        try:
            from src.monitoring.otel_metrics import get_metrics

            get_metrics().tool_errors.add(1, {"skill": skill_name, "error_type": error_type})
        except Exception:
            pass

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
            name = _sanitize_tool_name(tool_call.function.name)
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

        # Check per-skill circuit breaker
        breaker = self._get_breaker(name)
        if await breaker.is_open():
            log.warning(
                "Skill %r circuit breaker is OPEN — fast-failing",
                name,
                extra={"chat_id": chat_id, "skill": name},
            )
            self._audit(chat_id, name, raw_args, False, "circuit_open")
            return format_skill_error(
                skill_name=name,
                error_type="CircuitBreakerOpen",
                user_message="This tool is temporarily unavailable due to repeated failures.",
            )

        log.info(
            "━━━ ▶ EXECUTING SKILL '%s' ━━━  [chat: %s]",
            name,
            chat_id,
            extra={"chat_id": chat_id, "skill": name},
        )

        # ── Human-in-the-loop approval for dangerous skills ────────────────
        if getattr(skill, "dangerous", False) and self._send_message_fn is not None:
            args_preview = raw_args[:200]
            approved = await self._approval_manager.request_approval(
                chat_id=chat_id,
                skill_name=name,
                args_summary=args_preview,
                send_message=self._send_message_fn,
            )
            if not approved:
                log.info(
                    "Dangerous skill %r denied or timed out for chat %s",
                    name,
                    chat_id,
                    extra={"chat_id": chat_id, "skill": name},
                )
                self._audit(chat_id, name, raw_args, False, "approval_denied")
                return format_skill_error(
                    skill_name=name,
                    error_type="PermissionError",
                    user_message="Execution cancelled — approval was denied or timed out.",
                )

        # Execute with timeout and error handling
        try:
            async with skill_timer(
                skill_name=name,
                chat_id=chat_id,
                slow_threshold=SLOW_SKILL_THRESHOLD_SECONDS,
            ) as timing_result:
                # Build kwargs — inject send_media callback if the skill might use it
                exec_kwargs: dict[str, Any] = dict(args)
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
                    self._metrics.track_skill_result(name, success=True)
                    self._metrics.track_skill_timeout_ratio(
                        name, timing_result.duration_seconds, timeout
                    )
                log.info(
                    "━━━ ✔ SKILL '%s' DONE (%.1fs) ━━━  [chat: %s]",
                    name,
                    timing_result.duration_seconds,
                    chat_id,
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
                    await get_event_bus().emit(
                        Event(
                            name="skill_executed",
                            data={
                                "skill_name": name,
                                "chat_id": chat_id,
                                "duration_ms": round(timing_result.duration_ms, 2),
                            },
                            source="ToolExecutor",
                        )
                    )
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.EVENT_EMISSION,
                        f"Failed to emit skill_executed event for {name}",
                        logger=log,
                    )

                await breaker.record_success()
                return str(result) if result is not None else ""

        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await breaker.record_failure()
            if self._metrics:
                self._metrics.track_skill_error(name, "TimeoutError")
                self._metrics.track_skill_result(name, success=False)
            self._otel_track_tool_error(name, "TimeoutError")
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
            # Reuse the validated timeout from the try-block scope (already a
            # proper int/float — no second getattr needed).
            return format_skill_error(
                skill_name=name,
                error_type="TimeoutError",
                user_message=f"The operation took too long (timeout: {timeout}s).",
            )
        except SkillError as exc:
            await breaker.record_failure()
            error_type = exc.details.get("reason", "SkillError")
            user_message = get_user_friendly_message(exc.message, error_type)
            if self._metrics:
                self._metrics.track_skill_error(name, error_type)
                self._metrics.track_skill_result(name, success=False)
            self._otel_track_tool_error(name, error_type)
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
            await breaker.record_failure()
            error_type = type(exc).__name__
            if self._metrics:
                self._metrics.track_skill_error(name, error_type)
                self._metrics.track_skill_result(name, success=False)
            self._otel_track_tool_error(name, error_type)
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


def _measured_depth(obj: Any) -> int:
    """Return the maximum nesting depth of a JSON-parsed structure.

    Uses iterative traversal to avoid stack overflow on deeply nested inputs.
    Empty containers are skipped to avoid unnecessary tuple allocations.
    """
    max_depth = 0
    stack: list[tuple[Any, int]] = [(obj, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            if current:
                max_depth = max(max_depth, depth + 1)
                stack.extend(
                    (v, depth + 1)
                    for v in current.values()
                    if not (isinstance(v, (dict, list)) and not v)
                )
        elif isinstance(current, list):
            if current:
                max_depth = max(max_depth, depth + 1)
                stack.extend(
                    (v, depth + 1)
                    for v in current
                    if not (isinstance(v, (dict, list)) and not v)
                )
    return max_depth


# ── Error Formatting Functions ─────────────────────────────────────


def _truncate(value: str, max_len: int = _MAX_ERROR_FIELD_LEN) -> str:
    """Truncate a string to *max_len* characters, appending ellipsis if cut."""
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def format_skill_error(
    skill_name: str,
    error_type: str,
    user_message: str,
) -> str:
    """Format a user-friendly error message for skill failures."""
    corr_id = get_correlation_id()
    parts = [f"⚠️ {_truncate(user_message)}"]

    suggestion = get_error_suggestion(error_type)
    if suggestion:
        parts.append(f"💡 {suggestion}")

    ref_parts = [f"skill: {_truncate(skill_name)}", f"error: {_sanitize_error_type(error_type)}"]
    if corr_id:
        ref_parts.append(f"ref: {_truncate(corr_id)}")
    parts.append(f"🔢 {' | '.join(ref_parts)}")

    result = "\n".join(parts)
    if len(result) > _MAX_ERROR_RESPONSE_LEN:
        result = result[: _MAX_ERROR_RESPONSE_LEN - 1] + "…"
    return result


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
        "CircuitBreakerOpen": "This tool has failed repeatedly. Try again in a minute.",
    }
    return suggestions.get(error_type)
