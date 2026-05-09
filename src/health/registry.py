"""
src/health/registry.py — Centralized health check registry.

Provides a discoverable registry with standardized ``HealthCheck`` signatures,
replacing the ad-hoc ``validate_connection()`` / ``get_llm_status()`` /
``get_dedup_stats()`` scattered accessors.

Usage::

    from src.health.registry import HealthCheckRegistry

    registry = HealthCheckRegistry()
    registry.register(check_database, db=db_instance)
    registry.register(check_scheduler, scheduler=scheduler)

    report = await registry.run_all()
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from src.health.models import ComponentHealth, HealthReport, HealthStatus

log = logging.getLogger(__name__)

# A health check is any callable that returns ComponentHealth (sync or async).
HealthCheckFn = Callable[..., Any]


class HealthCheckRegistry:
    """Discoverable registry for health checks with error isolation.

    Each registered check is a callable (sync or async) that returns
    ``ComponentHealth``.  The registry captures dependencies at registration
    time and injects them when running the check, so callers never pass
    arguments at invocation time.

    Error isolation: a failing check produces a DEGRADED or UNHEALTHY
    ``ComponentHealth`` without preventing other checks from running.

    Per-check timeout: each check is wrapped in ``asyncio.wait_for()`` so
    that a hung check (e.g. LLM health probe waiting for TCP timeout) does
    not block the entire ``/health`` endpoint.
    """

    __slots__ = ("_checks",)

    def __init__(self) -> None:
        self._checks: list[tuple[str, HealthCheckFn, dict[str, Any]]] = []

    def register(
        self,
        fn: HealthCheckFn,
        *,
        name: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Register a health check with its bound dependencies.

        Args:
            fn: Sync or async callable returning ``ComponentHealth``.
            name: Override component name (defaults to fn.__name__).
            **kwargs: Dependencies injected into *fn* at check time.
        """
        component_name = name or getattr(fn, "__name__", "unknown")
        self._checks.append((component_name, fn, kwargs))

    def clear(self) -> None:
        """Remove all registered checks."""
        self._checks.clear()

    @property
    def check_names(self) -> list[str]:
        """Return the names of all registered checks."""
        return [name for name, _, _ in self._checks]

    async def run_all(
        self,
        *,
        version: str = "1.0.0",
        token_usage: dict[str, Any] | None = None,
        startup_durations: dict[str, float] | None = None,
        startup_total_seconds: float | None = None,
        token_cost: dict[str, Any] | None = None,
        check_timeout: float | None = None,
    ) -> HealthReport:
        """Run all registered checks and return an aggregated report.

        Checks are executed sequentially.  Each check is individually
        wrapped in try/except so a single failure does not prevent
        other checks from running.

        Args:
            check_timeout: Per-check timeout in seconds.  ``None`` uses
                the module-level default (``DEFAULT_HEALTH_CHECK_TIMEOUT``).
                A check exceeding this limit is cancelled and reported as
                DEGRADED.
        """
        from src.constants.health import DEFAULT_HEALTH_CHECK_TIMEOUT

        timeout = check_timeout if check_timeout is not None else DEFAULT_HEALTH_CHECK_TIMEOUT
        components: list[ComponentHealth] = []

        for name, fn, kwargs in self._checks:
            component = await self._run_one(name, fn, kwargs, timeout)
            components.append(component)

        return HealthReport(
            components=components,
            version=version,
            token_usage=token_usage,
            startup_durations=startup_durations,
            startup_total_seconds=startup_total_seconds,
            token_cost=token_cost,
        )

    async def _run_one(
        self,
        name: str,
        fn: HealthCheckFn,
        kwargs: dict[str, Any],
        timeout: float,
    ) -> ComponentHealth:
        """Execute a single health check with error and timeout isolation."""
        try:
            result = fn(**kwargs)
            if asyncio.iscoroutine(result) or asyncio.isfuture(result):
                result = await asyncio.wait_for(result, timeout=timeout)
            if not isinstance(result, ComponentHealth):
                log.warning(
                    "Health check %r returned %s, expected ComponentHealth",
                    name,
                    type(result).__name__,
                )
                return ComponentHealth(
                    name=name,
                    status=HealthStatus.DEGRADED,
                    message=f"Check returned unexpected type: {type(result).__name__}",
                )
            return result
        except asyncio.TimeoutError:
            log.warning("Health check %r timed out after %.1fs", name, timeout)
            return ComponentHealth(
                name=name,
                status=HealthStatus.DEGRADED,
                message=f"Check timed out after {timeout:.1f}s",
            )
        except Exception as exc:
            log.debug("Health check %r failed: %s", name, exc)
            return ComponentHealth(
                name=name,
                status=HealthStatus.DEGRADED,
                message=f"Check failed: {type(exc).__name__}",
            )
