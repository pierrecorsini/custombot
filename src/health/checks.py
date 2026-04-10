"""
src/health/checks.py — Individual health check functions.

Each function checks one component and returns a ComponentHealth result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from src.health.models import ComponentHealth, HealthStatus

if TYPE_CHECKING:
    from src.db import Database
    from src.channels.whatsapp import NeonizeBackend

log = logging.getLogger(__name__)


async def check_database(db: "Database") -> ComponentHealth:
    """Check if the database is accessible."""
    start = time.perf_counter()
    try:
        if not db._initialized:
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message="Database not initialized",
            )
        _ = len(db._chats)
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="database",
            status=HealthStatus.HEALTHY,
            message="Database is accessible",
            latency_ms=latency,
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        log.warning("Database health check failed: %s", e)
        return ComponentHealth(
            name="database",
            status=HealthStatus.UNHEALTHY,
            message=f"Database error: {e}",
            latency_ms=latency,
        )


async def check_neonize(backend: Optional["NeonizeBackend"]) -> ComponentHealth:
    """Check neonize WhatsApp connection state."""
    start = time.perf_counter()
    try:
        if backend is None:
            return ComponentHealth(
                name="whatsapp",
                status=HealthStatus.UNHEALTHY,
                message="WhatsApp backend not configured",
            )
        connected = backend.is_connected
        latency = (time.perf_counter() - start) * 1000
        if connected:
            return ComponentHealth(
                name="whatsapp",
                status=HealthStatus.HEALTHY,
                message="WhatsApp connected",
                latency_ms=latency,
            )
        return ComponentHealth(
            name="whatsapp",
            status=HealthStatus.UNHEALTHY,
            message="WhatsApp not connected",
            latency_ms=latency,
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="whatsapp",
            status=HealthStatus.UNHEALTHY,
            message=f"WhatsApp check failed: {type(e).__name__}",
            latency_ms=latency,
        )


async def check_llm_credentials(
    api_key: str, base_url: str, timeout: float = 5.0
) -> ComponentHealth:
    """Check if LLM credentials are valid by making a minimal API call."""
    if not api_key or api_key.startswith("sk-your"):
        return ComponentHealth(
            name="llm",
            status=HealthStatus.UNHEALTHY,
            message="API key not configured",
        )

    start = time.perf_counter()
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        await asyncio.wait_for(client.models.list(), timeout=timeout)
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="llm",
            status=HealthStatus.HEALTHY,
            message="LLM credentials are valid",
            latency_ms=latency,
        )
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="llm",
            status=HealthStatus.DEGRADED,
            message="LLM API timeout (credentials may be valid)",
            latency_ms=latency,
        )
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        error_msg = str(e).lower()
        if "401" in error_msg or "unauthorized" in error_msg or "invalid" in error_msg:
            return ComponentHealth(
                name="llm",
                status=HealthStatus.UNHEALTHY,
                message="Invalid API credentials",
                latency_ms=latency,
            )
        log.debug("LLM health check error: %s", e)
        return ComponentHealth(
            name="llm",
            status=HealthStatus.DEGRADED,
            message=f"LLM check failed: {type(e).__name__}",
            latency_ms=latency,
        )


def get_token_usage_stats() -> dict[str, Any]:
    """Get LLM token usage statistics for the current session."""
    try:
        from src.llm import get_token_usage

        usage = get_token_usage()
        return {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
            "request_count": usage.request_count,
        }
    except Exception:
        return {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "request_count": 0,
            "error": "Token tracking not available",
        }
