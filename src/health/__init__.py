"""
src/health/ — Health check system.

Split into focused modules:
- checks: Individual health check functions (database, WhatsApp, LLM)
- server: HTTP server with /health endpoint
- models: Data classes (HealthStatus, ComponentHealth, HealthReport)
"""

from src.health.models import HealthStatus, ComponentHealth, HealthReport
from src.health.checks import (
    check_database,
    check_neonize,
    check_llm_credentials,
    get_token_usage_stats,
)
from src.health.server import HealthServer, run_health_server

__all__ = [
    "HealthStatus",
    "ComponentHealth",
    "HealthReport",
    "check_database",
    "check_neonize",
    "check_llm_credentials",
    "get_token_usage_stats",
    "HealthServer",
    "run_health_server",
]
