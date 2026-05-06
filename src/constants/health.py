"""Health server constants — rate limiting, disk-space thresholds, request limits."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Health Server Rate Limiting
# ─────────────────────────────────────────────────────────────────────────────

# Maximum HTTP requests per IP to the health server within the rate-limit window.
HEALTH_HTTP_RATE_LIMIT: int = 60

# Sliding window size (seconds) for health server rate limiting.
HEALTH_HTTP_RATE_WINDOW_SECONDS: float = 60.0

# Maximum distinct IPs to track for health server rate limiting.
HEALTH_HTTP_RATE_MAX_TRACKED_IPS: int = 500

# ─────────────────────────────────────────────────────────────────────────────
# Health Check — Disk Space
# ─────────────────────────────────────────────────────────────────────────────

# Minimum free disk space (MB) for the health endpoint to report HEALTHY.
# Below this threshold the component status is DEGRADED, signalling that
# writes to JSONL, vector memory, or the message queue may start failing.
HEALTH_DISK_FREE_THRESHOLD_MB: float = 500.0

# Maximum allowed request body size (bytes) for the health server.
# Health endpoints only serve short GET requests with no body; any POST
# or PUT payload exceeding this is rejected to prevent memory exhaustion.
HEALTH_MAX_REQUEST_BODY_BYTES: int = 1024  # 1 KB

# Maximum allowed URL path length (characters) for the health server.
# Legitimate paths are short (/, /health, /metrics, /ready, /version).
# Excessively long paths are rejected to prevent memory exhaustion.
HEALTH_MAX_URL_LENGTH: int = 2048  # 2 KB

# Allowed URL paths for the health server.
# Requests to any other path are rejected immediately with 404, preventing
# cache-poisoning, log noise, and wasted middleware processing from arbitrary
# URL probes.  Query strings are not considered part of the path.
HEALTH_ALLOWED_PATHS: frozenset[str] = frozenset({"/", "/health", "/ready", "/version", "/metrics"})
