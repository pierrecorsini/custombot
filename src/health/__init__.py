"""
src/health/ — Health check system.

Split into focused modules:
- checks: Individual health check functions (database, WhatsApp, LLM)
- server: HTTP server with /health endpoint
- models: Data classes (HealthStatus, ComponentHealth, HealthReport)
- registry: Centralized health check registry with error isolation
- prometheus: Prometheus text exposition format renderer
- middleware: HTTP middleware (rate limiting, HMAC auth, request size limits)
"""

from src.health.checks import (
    check_database,
    check_disk_usage,
    check_llm_credentials,
    check_neonize,
    check_scheduler,
    get_token_usage_stats,
)
from src.health.models import ComponentHealth, HealthReport, HealthStatus
from src.health.registry import HealthCheckRegistry
from src.health.server import HealthServer, run_health_server

__all__ = [
    "HealthStatus",
    "ComponentHealth",
    "HealthReport",
    "HealthCheckRegistry",
    "check_database",
    "check_disk_usage",
    "check_neonize",
    "check_llm_credentials",
    "check_scheduler",
    "get_token_usage_stats",
    "HealthServer",
    "run_health_server",
]
