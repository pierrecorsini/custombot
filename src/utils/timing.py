"""
timing.py — Timing utilities for performance monitoring.

Provides context managers for measuring execution duration of operations,
particularly skill executions. Supports both synchronous and asynchronous
contexts.

Usage:
    async with skill_timer("read_file", chat_id="123") as timer:
        result = await some_operation()
    # Duration automatically logged on exit

    with OperationTimer("sync_op") as timer:
        do_something()
    print(f"Duration: {timer.duration_ms:.2f}ms")
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Default threshold for slow operation warnings (in seconds).
DEFAULT_SLOW_THRESHOLD_SECONDS: float = 5.0


# ─────────────────────────────────────────────────────────────────────────────
# Data Classes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class TimingResult:
    """
    Result of a timed operation.

    Attributes:
        operation_name: Name of the operation being timed.
        duration_ms: Duration in milliseconds.
        success: Whether the operation completed successfully.
        error: Error message if operation failed, None otherwise.
        metadata: Additional context (e.g., chat_id, skill_name).
    """

    operation_name: str
    duration_ms: float
    success: bool = True
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Duration in seconds."""
        return self.duration_ms / 1000.0

    def to_log_extra(self) -> dict[str, Any]:
        """Convert to structured logging extra dict."""
        extra = {
            "operation": self.operation_name,
            "duration_ms": round(self.duration_ms, 2),
            "success": self.success,
        }
        extra.update(self.metadata)
        if self.error:
            extra["error"] = self.error
        return extra


# ─────────────────────────────────────────────────────────────────────────────
# Synchronous Timer
# ─────────────────────────────────────────────────────────────────────────────


class OperationTimer:
    """
    Synchronous context manager for timing operations.

    Measures elapsed time using perf_counter for high precision.
    Can be used directly or via the @timed decorator.

    Attributes:
        name: Operation name for logging.
        metadata: Additional context for structured logging.
        slow_threshold: Seconds before warning is logged.

    Example:
        with OperationTimer("database_query", query_id="abc") as timer:
            result = db.execute(query)
        # Logs: "database_query completed in 123.45ms"
    """

    __slots__ = ("_start", "_end", "metadata", "name", "result", "slow_threshold")

    def __init__(
        self,
        name: str,
        slow_threshold: float = DEFAULT_SLOW_THRESHOLD_SECONDS,
        **metadata: Any,
    ) -> None:
        """
        Initialize the timer.

        Args:
            name: Operation name for logging.
            slow_threshold: Seconds before warning is logged.
            **metadata: Additional context for structured logging.
        """
        self.name = name
        self.slow_threshold = slow_threshold
        self.metadata = metadata
        self._start: float = 0.0
        self._end: float = 0.0
        self.result: TimingResult | None = None

    def __enter__(self) -> "OperationTimer":
        """Start timing."""
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool:
        """Stop timing and log result."""
        self._end = time.perf_counter()
        duration_ms = (self._end - self._start) * 1000

        success = exc_type is None
        error_msg = str(exc_val) if exc_val else None

        self.result = TimingResult(
            operation_name=self.name,
            duration_ms=duration_ms,
            success=success,
            error=error_msg,
            metadata=self.metadata,
        )

        self._log_result()
        return False  # Don't suppress exceptions

    @property
    def duration_ms(self) -> float:
        """Get duration in milliseconds (only valid after context exit)."""
        if self.result is None:
            raise RuntimeError("Timer has not completed yet")
        return self.result.duration_ms

    def _log_result(self) -> None:
        """Log the timing result."""
        if self.result is None:
            return

        extra = self.result.to_log_extra()
        duration_s = self.result.duration_seconds

        if not self.result.success:
            log.error(
                "%s failed after %.2fms: %s",
                self.name,
                self.result.duration_ms,
                self.result.error,
                extra=extra,
            )
        elif duration_s >= self.slow_threshold:
            log.warning(
                "%s completed in %.2fms (slow, threshold=%.1fs)",
                self.name,
                self.result.duration_ms,
                self.slow_threshold,
                extra=extra,
            )
        else:
            log.debug(
                "%s completed in %.2fms",
                self.name,
                self.result.duration_ms,
                extra=extra,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Async Timer (Context Manager)
# ─────────────────────────────────────────────────────────────────────────────


@asynccontextmanager
async def skill_timer(
    skill_name: str,
    chat_id: str | None = None,
    slow_threshold: float = DEFAULT_SLOW_THRESHOLD_SECONDS,
    **extra_metadata: Any,
) -> AsyncIterator[TimingResult]:
    """
    Async context manager for timing skill executions.

    Logs skill name, duration, and success/failure status.
    Warns on slow executions exceeding the threshold.

    Args:
        skill_name: Name of the skill being executed.
        chat_id: Optional chat ID for correlation.
        slow_threshold: Seconds before warning is logged.
        **extra_metadata: Additional context for structured logging.

    Yields:
        TimingResult that will be populated after the context exits.

    Example:
        async with skill_timer("read_file", chat_id="123") as result:
            content = await file.read()
        # Logs: "read_file completed in 123.45ms"
        print(f"Duration: {result.duration_ms:.2f}ms")
    """
    metadata = {"skill": skill_name}
    if chat_id:
        metadata["chat_id"] = chat_id
    metadata.update(extra_metadata)

    start = time.perf_counter()
    result = TimingResult(
        operation_name=skill_name,
        duration_ms=0.0,
        metadata=metadata,
    )

    try:
        yield result
        result.success = True
    except Exception as exc:
        result.success = False
        result.error = str(exc)
        raise
    finally:
        end = time.perf_counter()
        result.duration_ms = (end - start) * 1000
        _log_skill_timing(result, slow_threshold)


def _log_skill_timing(result: TimingResult, slow_threshold: float) -> None:
    """Log skill timing result with appropriate log level."""
    extra = result.to_log_extra()
    duration_s = result.duration_seconds

    if not result.success:
        log.error(
            "Skill %r failed after %.2fms: %s",
            result.operation_name,
            result.duration_ms,
            result.error,
            extra=extra,
        )
    elif duration_s >= slow_threshold:
        log.warning(
            "Skill %r completed in %.2fms (slow, threshold=%.1fs)",
            result.operation_name,
            result.duration_ms,
            slow_threshold,
            extra=extra,
        )
    else:
        log.info(
            "Skill %r completed in %.2fms",
            result.operation_name,
            result.duration_ms,
            extra=extra,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Synchronous Context Manager Factory
# ─────────────────────────────────────────────────────────────────────────────


@contextmanager
def timed_operation(
    name: str,
    slow_threshold: float = DEFAULT_SLOW_THRESHOLD_SECONDS,
    **metadata: Any,
) -> Iterator[TimingResult]:
    """
    Synchronous context manager for timing operations.

    Args:
        name: Operation name for logging.
        slow_threshold: Seconds before warning is logged.
        **metadata: Additional context for structured logging.

    Yields:
        TimingResult that will be populated after the context exits.

    Example:
        with timed_operation("db_query", query="SELECT...") as result:
            rows = db.execute(query)
        print(f"Duration: {result.duration_ms:.2f}ms")
    """
    start = time.perf_counter()
    result = TimingResult(
        operation_name=name,
        duration_ms=0.0,
        metadata=metadata,
    )

    try:
        yield result
        result.success = True
    except Exception as exc:
        result.success = False
        result.error = str(exc)
        raise
    finally:
        end = time.perf_counter()
        result.duration_ms = (end - start) * 1000

        extra = result.to_log_extra()
        duration_s = result.duration_seconds

        if not result.success:
            log.error(
                "%s failed after %.2fms: %s",
                name,
                result.duration_ms,
                result.error,
                extra=extra,
            )
        elif duration_s >= slow_threshold:
            log.warning(
                "%s completed in %.2fms (slow, threshold=%.1fs)",
                name,
                result.duration_ms,
                slow_threshold,
                extra=extra,
            )
        else:
            log.debug(
                "%s completed in %.2fms",
                name,
                result.duration_ms,
                extra=extra,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "OperationTimer",
    "TimingResult",
    "skill_timer",
    "timed_operation",
    "DEFAULT_SLOW_THRESHOLD_SECONDS",
]
