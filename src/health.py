"""Backward-compat re-export — module split into src.health/ package."""

import warnings

warnings.warn(
    "Importing from 'src.health' is deprecated. "
    "Import from 'src.health.models', 'src.health.checks', or 'src.health.server' instead.",
    DeprecationWarning,
    stacklevel=2,
)

from src.health.checks import (  # noqa: F401
    check_database,
    check_llm_credentials,
    check_neonize,
    get_token_usage_stats,
)
from src.health.models import ComponentHealth, HealthStatus  # noqa: F401
from src.health.server import HealthServer, run_health_server  # noqa: F401
