"""
logging_utils.py — Decorator for logging function execution.

Provides @log_execution decorator that logs function entry/exit with timing.
Supports both sync and async functions.

Usage:
    @log_execution(level="info", log_args=True)
    async def process_message(chat_id: str, text: str):
        ...

    @log_execution(log_result=True)
    def calculate_total(items: list) -> int:
        ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from typing import Any, Callable, Optional, TypeVar, Union, cast

F = TypeVar("F", bound=Callable[..., Any])

# Mapping of level names to logging constants
_LEVEL_MAP = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def _resolve_level(level: Union[str, int]) -> int:
    """Resolve log level from string or int."""
    if isinstance(level, int):
        return level
    return _LEVEL_MAP.get(level.lower(), logging.DEBUG)


def _format_args(args: tuple, kwargs: dict) -> str:
    """Format function arguments for logging."""
    parts = [repr(a) for a in args]
    parts.extend(f"{k}={v!r}" for k, v in kwargs.items())
    return ", ".join(parts)


def _truncate(value: str, max_len: int = 200) -> str:
    """Truncate string for logging."""
    if len(value) <= max_len:
        return value
    return value[:max_len] + "..."


def log_execution(
    level: str = "debug",
    log_args: bool = True,
    log_result: bool = False,
    logger: Optional[logging.Logger] = None,
) -> Callable[[F], F]:
    """
    Decorator that logs function entry/exit with timing.

    Args:
        level: Log level - "debug", "info", "warning", or "error" (default: "debug")
        log_args: Whether to log function arguments (default: True)
        log_result: Whether to log function result (default: False)
        logger: Custom logger instance (default: uses function's module logger)

    Returns:
        Decorated function with logging

    Example:
        @log_execution(level="info", log_args=True)
        async def process_message(chat_id: str, text: str):
            return await api.send(chat_id, text)
    """

    def decorator(func: F) -> F:
        log = logger or logging.getLogger(func.__module__)
        log_level = _resolve_level(level)

        if asyncio.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                func_name = func.__qualname__

                # Log entry
                if log_args:
                    args_str = _truncate(_format_args(args, kwargs))
                    log.log(log_level, "%s(%s) -> starting", func_name, args_str)
                else:
                    log.log(log_level, "%s -> starting", func_name)

                start = time.perf_counter()
                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start) * 1000

                    # Log success
                    if log_result:
                        result_str = _truncate(repr(result))
                        log.log(
                            log_level,
                            "%s -> completed in %.2fms, result=%s",
                            func_name,
                            duration_ms,
                            result_str,
                        )
                    else:
                        log.log(
                            log_level,
                            "%s -> completed in %.2fms",
                            func_name,
                            duration_ms,
                        )

                    return result

                except Exception as exc:
                    duration_ms = (time.perf_counter() - start) * 1000
                    log.error(
                        "%s -> failed after %.2fms: %s: %s",
                        func_name,
                        duration_ms,
                        type(exc).__name__,
                        exc,
                    )
                    raise

            return cast(F, async_wrapper)
        else:

            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                func_name = func.__qualname__

                # Log entry
                if log_args:
                    args_str = _truncate(_format_args(args, kwargs))
                    log.log(log_level, "%s(%s) -> starting", func_name, args_str)
                else:
                    log.log(log_level, "%s -> starting", func_name)

                start = time.perf_counter()
                try:
                    result = func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - start) * 1000

                    # Log success
                    if log_result:
                        result_str = _truncate(repr(result))
                        log.log(
                            log_level,
                            "%s -> completed in %.2fms, result=%s",
                            func_name,
                            duration_ms,
                            result_str,
                        )
                    else:
                        log.log(
                            log_level,
                            "%s -> completed in %.2fms",
                            func_name,
                            duration_ms,
                        )

                    return result

                except Exception as exc:
                    duration_ms = (time.perf_counter() - start) * 1000
                    log.error(
                        "%s -> failed after %.2fms: %s: %s",
                        func_name,
                        duration_ms,
                        type(exc).__name__,
                        exc,
                    )
                    raise

            return cast(F, sync_wrapper)

    return decorator


__all__ = ["log_execution"]
