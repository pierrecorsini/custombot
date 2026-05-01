"""
src/health/models.py — Data classes for health reporting.

Defines HealthStatus enum, ComponentHealth dataclass,
and HealthReport aggregate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class HealthStatus(str, Enum):
    """Health check status values."""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"


@dataclass(slots=True)
class ComponentHealth:
    """Health status of a single component."""

    name: str
    status: HealthStatus
    message: str = ""
    latency_ms: Optional[float] = None
    details: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": self.status.value,
            "message": self.message,
        }
        if self.latency_ms is not None:
            result["latency_ms"] = round(self.latency_ms, 2)
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(slots=True)
class HealthReport:
    """Overall health report for the bot."""

    status: HealthStatus
    components: list[ComponentHealth] = field(default_factory=list)
    version: str = "1.0.0"
    token_usage: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        overall = HealthStatus.HEALTHY
        for comp in self.components:
            if comp.status == HealthStatus.UNHEALTHY:
                overall = HealthStatus.UNHEALTHY
                break
            if comp.status == HealthStatus.DEGRADED and overall == HealthStatus.HEALTHY:
                overall = HealthStatus.DEGRADED

        result: dict[str, Any] = {
            "status": overall.value,
            "version": self.version,
            "components": {c.name: c.to_dict() for c in self.components},
        }
        if self.token_usage is not None:
            result["token_usage"] = self.token_usage
        return result
