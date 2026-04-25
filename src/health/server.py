"""
src/health/server.py — HTTP health check and metrics endpoints for monitoring.

Provides a lightweight HTTP server with:
- /health  — JSON health check for service status
- /ready   — Kubernetes-style readiness probe (200 when fully initialized)
- /metrics — Prometheus-compatible metrics endpoint
- /version — Bot version and Python runtime info

Optional HMAC authentication via ``HEALTH_HMAC_SECRET`` env var:
- If set, unauthenticated requests receive only a basic status code (200/503).
- Authenticated requests (``Authorization: HMAC-SHA256 <timestamp>:<signature>``)
  receive the full detailed response.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from threading import Lock
from typing import TYPE_CHECKING, Any, Optional

from src.health.checks import (
    check_database,
    check_disk_usage,
    check_disk_space_health,
    check_llm_credentials,
    check_llm_logs,
    check_neonize,
    check_scheduler,
    check_wiring,
    get_token_usage_stats,
)
from src.health.models import ComponentHealth, HealthReport, HealthStatus
from src.rate_limiter import SlidingWindowTracker
from src.utils import BoundedOrderedDict

if TYPE_CHECKING:
    from src.bot import Bot
    from src.channels.neonize_backend import NeonizeBackend
    from src.db import Database
    from src.scheduler import TaskScheduler
    from src.shutdown import GracefulShutdown

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Per-IP Rate Limiting for Health Server
# ─────────────────────────────────────────────────────────────────────────────


def _load_rate_limit_config() -> tuple[int, float, int]:
    """Load health HTTP rate limit settings from env or defaults."""
    from src.constants import (
        HEALTH_HTTP_RATE_LIMIT,
        HEALTH_HTTP_RATE_MAX_TRACKED_IPS,
        HEALTH_HTTP_RATE_WINDOW_SECONDS,
    )

    limit = os.environ.get("HEALTH_HTTP_RATE_LIMIT", "")
    window = os.environ.get("HEALTH_HTTP_RATE_WINDOW", "")
    max_ips = os.environ.get("HEALTH_HTTP_RATE_MAX_IPS", "")

    return (
        int(limit) if limit.isdigit() else HEALTH_HTTP_RATE_LIMIT,
        float(window) if window else HEALTH_HTTP_RATE_WINDOW_SECONDS,
        int(max_ips) if max_ips.isdigit() else HEALTH_HTTP_RATE_MAX_TRACKED_IPS,
    )


class _IPLimiter:
    """Per-IP sliding window rate limiter with LRU eviction."""

    def __init__(self, limit: int, window_seconds: float, max_ips: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._trackers: BoundedOrderedDict[str, SlidingWindowTracker] = (
            BoundedOrderedDict(max_size=max_ips, eviction="half")
        )
        self._lock = Lock()

    def _get_tracker(self, ip: str) -> SlidingWindowTracker:
        with self._lock:
            if ip in self._trackers:
                return self._trackers[ip]
            tracker = SlidingWindowTracker(self._window_seconds, self._limit)
            self._trackers[ip] = tracker
            return tracker

    def check(self, ip: str) -> tuple[bool, int, float]:
        """Check rate limit for an IP. Returns (allowed, remaining, retry_after)."""
        tracker = self._get_tracker(ip)
        allowed, remaining, retry_after = tracker.check_only()
        if not allowed:
            return False, 0, retry_after
        tracker.record()
        return True, remaining, 0.0


def _create_rate_limit_middleware(limiter: _IPLimiter) -> Any:
    """Create an aiohttp middleware that rate-limits by client IP."""
    from aiohttp import web

    @web.middleware
    async def rate_limit_middleware(request: web.Request, handler: Any) -> Any:
        # Extract client IP — use X-Forwarded-For if behind a proxy, else remote
        forwarded = request.headers.get("X-Forwarded-For", "")
        ip = forwarded.split(",")[0].strip() if forwarded else request.remote or "unknown"

        allowed, remaining, retry_after = limiter.check(ip)
        if not allowed:
            log.warning("Health server rate limit exceeded for IP %s", ip)
            resp = web.Response(
                text="Too Many Requests",
                status=429,
                content_type="text/plain",
            )
            resp.headers["Retry-After"] = str(int(retry_after) + 1)
            return resp

        resp = await handler(request)
        resp.headers["X-RateLimit-Remaining"] = str(remaining)
        return resp

    return rate_limit_middleware


# ─────────────────────────────────────────────────────────────────────────────
# Request Size Limits
# ─────────────────────────────────────────────────────────────────────────────


def _create_method_validation_middleware() -> Any:
    """Create an aiohttp middleware that rejects non-GET/HEAD/OPTIONS requests.

    Health endpoints are read-only.  Rejecting write methods (POST, PUT,
    DELETE, PATCH) before they reach HMAC verification or handler logic
    reduces the attack surface.
    """
    from aiohttp import web

    _ALLOWED = frozenset({"GET", "HEAD", "OPTIONS"})

    @web.middleware
    async def method_validation_middleware(
        request: web.Request, handler: Any
    ) -> Any:
        if request.method not in _ALLOWED:
            log.warning(
                "Health server rejected %s request to %s",
                request.method,
                request.path,
            )
            return web.Response(
                text="Method Not Allowed",
                status=405,
                content_type="text/plain",
            )
        return await handler(request)

    return method_validation_middleware


def _load_request_size_config() -> tuple[int, int]:
    """Load health HTTP request size limits from env or defaults."""
    from src.constants import HEALTH_MAX_REQUEST_BODY_BYTES, HEALTH_MAX_URL_LENGTH

    body_str = os.environ.get("HEALTH_MAX_BODY_BYTES", "")
    url_str = os.environ.get("HEALTH_MAX_URL_LENGTH", "")

    return (
        int(body_str) if body_str.isdigit() else HEALTH_MAX_REQUEST_BODY_BYTES,
        int(url_str) if url_str.isdigit() else HEALTH_MAX_URL_LENGTH,
    )


def _create_request_size_limit_middleware(
    max_body_bytes: int, max_url_length: int
) -> Any:
    """Create an aiohttp middleware that rejects oversized requests.

    Health endpoints serve short GET requests.  Rejecting bodies > *max_body_bytes*
    and URL paths > *max_url_length* prevents memory exhaustion from malicious or
    misconfigured clients.
    """
    from aiohttp import web

    @web.middleware
    async def request_size_limit_middleware(
        request: web.Request, handler: Any
    ) -> Any:
        if len(request.path) > max_url_length:
            log.warning(
                "Health server rejected oversized URL path (%d chars, max %d)",
                len(request.path),
                max_url_length,
            )
            return web.Response(
                text="URI Too Long",
                status=414,
                content_type="text/plain",
            )

        content_length = request.content_length
        if content_length is not None and content_length > max_body_bytes:
            log.warning(
                "Health server rejected oversized request body (%d bytes, max %d)",
                content_length,
                max_body_bytes,
            )
            return web.Response(
                text="Payload Too Large",
                status=413,
                content_type="text/plain",
            )

        return await handler(request)

    return request_size_limit_middleware


# ─────────────────────────────────────────────────────────────────────────────
# HMAC Request Verification
# ─────────────────────────────────────────────────────────────────────────────

_HMAC_MAX_SKEW_SECONDS = 300  # 5 minutes


def _load_hmac_secret() -> str | None:
    """Load the optional HMAC secret from the environment."""
    secret = os.environ.get("HEALTH_HMAC_SECRET", "").strip()
    return secret if secret else None


def _mask_hmac_header(value: str) -> str:
    """Redact the HMAC token portion of an Authorization header.

    Keeps the scheme prefix (``HMAC-SHA256``) so log analysts can see the
    authentication *method* without the secret material.
    """
    if value.startswith("HMAC-SHA256 "):
        return "HMAC-SHA256 [REDACTED]"
    # Non-HMAC auth header — still redact the credential portion
    if len(value) > 12:
        return value[:12] + "...[REDACTED]"
    return "[REDACTED]"


class _SecretRedactingFilter(logging.Filter):
    """Log filter that redacts HMAC credential tokens and the raw secret from log output.

    Defense-in-depth: even if a log statement accidentally includes the
    ``Authorization`` header value or the raw ``HEALTH_HMAC_SECRET``,
    the credential portion is replaced with ``[REDACTED]`` before the
    record reaches any handler.
    """

    _HMAC_PATTERN = re.compile(r"HMAC-SHA256\s+\S+")

    def __init__(self, secret: str | None = None) -> None:
        super().__init__()
        # Build a regex that matches the raw secret value so that even if it
        # appears in an unexpected log field (query string, custom header,
        # aiohttp DEBUG access log) it is scrubbed.
        self._secret_pattern: re.Pattern[str] | None = None
        if secret and len(secret) >= 4:
            escaped = re.escape(secret)
            self._secret_pattern = re.compile(escaped)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(self._redact(a) for a in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._redact(v) for k, v in record.args.items()}
        return True

    def _redact(self, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = self._HMAC_PATTERN.sub("HMAC-SHA256 [REDACTED]", value)
        if self._secret_pattern is not None:
            value = self._secret_pattern.sub("[REDACTED]", value)
        return value


def _verify_hmac(request: Any, secret: str) -> bool:
    """Verify HMAC-SHA256 signature on an incoming request.

    Expected header format::

        Authorization: HMAC-SHA256 <timestamp>:<hex-signature>

    Where ``signature = HMAC-SHA256(secret, f"{timestamp}{method}{path}")``.
    Uses ``hmac.compare_digest`` for timing-safe comparison.
    Rejects timestamps older than ``_HMAC_MAX_SKEW_SECONDS``.

    Both the expected and provided signatures are normalized to a fixed
    length before comparison to ensure constant-time behaviour regardless
    of input length differences.
    """
    from aiohttp import web

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("HMAC-SHA256 "):
        return False

    token = auth_header[len("HMAC-SHA256 "):]
    if ":" not in token:
        return False

    timestamp_str, signature = token.split(":", 1)
    try:
        timestamp = float(timestamp_str)
    except (ValueError, TypeError):
        return False

    if abs(time.time() - timestamp) > _HMAC_MAX_SKEW_SECONDS:
        log.warning("HMAC verification failed: timestamp expired")
        return False

    method = request.method
    path = request.path
    message = f"{timestamp_str}{method}{path}"
    expected = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    # Normalize to equal-length strings before comparison so that
    # hmac.compare_digest operates in constant time regardless of length
    # differences between the expected and provided signatures.
    _fixed_len = 128  # SHA-256 hex digest is 64 chars; pad generously
    expected_padded = expected.ljust(_fixed_len)
    signature_padded = signature.ljust(_fixed_len)

    if not hmac.compare_digest(expected_padded, signature_padded):
        log.warning("HMAC verification failed: invalid signature")
        return False

    return True


def _create_hmac_middleware(secret: str) -> Any:
    """Create an aiohttp middleware that enforces HMAC authentication.

    When the secret is set:
    - Authenticated requests → full detailed response (pass-through).
    - Unauthenticated requests to ``/health`` or ``/ready`` → minimal
      status code only (200 or 503) with no body details.
    - Unauthenticated requests to ``/metrics`` → HTTP 401.
    """
    from aiohttp import web

    @web.middleware
    async def hmac_middleware(request: web.Request, handler: Any) -> Any:
        # Capture and verify the original header before masking it.
        auth_value = request.headers.get("Authorization")
        authenticated = auth_value is not None and _verify_hmac(request, secret)

        # Mask the Authorization header in-place so downstream logging,
        # access-log middleware, and error handlers never see the raw
        # HMAC token in full.
        if auth_value is not None:
            request.headers["Authorization"] = _mask_hmac_header(auth_value)

        if authenticated:
            return await handler(request)

        # Unauthenticated: return minimal response based on path
        path = request.path
        if path == "/metrics":
            return web.Response(
                text="Unauthorized",
                status=401,
                content_type="text/plain",
            )

        if path in ("/health", "/ready"):
            # Return bare status code — handler still runs to determine
            # healthy vs unhealthy, but we strip the body.
            resp = await handler(request)
            return web.Response(status=resp.status)

        # Other paths (e.g. /) — pass through
        return await handler(request)

    return hmac_middleware


# ─────────────────────────────────────────────────────────────────────────────
# PII Redaction
# ─────────────────────────────────────────────────────────────────────────────


def _redact_chat_id(chat_id: str) -> str:
    """Hash a chat_id for Prometheus labels to avoid exposing PII (phone numbers)."""
    return hashlib.sha256(chat_id.encode()).hexdigest()[:8]


# ─────────────────────────────────────────────────────────────────────────────
# Prometheus Text Format Renderer
# ─────────────────────────────────────────────────────────────────────────────


def _format_prometheus_metric(
    name: str,
    help_text: str,
    metric_type: str,
    value: float | int,
    labels: dict[str, str] | None = None,
) -> str:
    """Format a single Prometheus metric line."""
    label_str = ""
    if labels:
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        label_str = "{" + ",".join(parts) + "}"
    return (
        f"# HELP {name} {help_text}\n"
        f"# TYPE {name} {metric_type}\n"
        f"{name}{label_str} {value}\n"
    )


def _format_prometheus_summary(
    name: str,
    help_text: str,
    count: int,
    sum_ms: float | None = None,
    quantiles: dict[str, float] | None = None,
    labels: dict[str, str] | None = None,
) -> str:
    """Format a Prometheus summary metric with quantiles and optional labels."""
    # Build label prefix for quantile lines (trailing comma) and full label
    # string for _sum/_count lines (no trailing comma)
    quantile_prefix = ""
    suffix_labels = ""
    if labels:
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        joined = ",".join(parts)
        quantile_prefix = f"{joined},"
        suffix_labels = f"{{{joined}}}"

    lines = [
        f"# HELP {name} {help_text}\n",
        f"# TYPE {name} summary\n",
    ]
    if quantiles:
        for q_label, q_val in sorted(quantiles.items()):
            lines.append(f'{name}{{{quantile_prefix}quantile="{q_label}"}} {q_val}\n')
    if sum_ms is not None:
        sum_suffix = f"_sum{suffix_labels}" if suffix_labels else "_sum"
        lines.append(f"{name}{sum_suffix} {sum_ms}\n")
    count_suffix = f"_count{suffix_labels}" if suffix_labels else "_count"
    lines.append(f"{name}{count_suffix} {count}\n")
    return "".join(lines)


def _format_prometheus_histogram(
    name: str,
    help_text: str,
    histogram: dict[str, Any],
    labels: dict[str, str] | None = None,
) -> str:
    """Format a Prometheus histogram metric with ``le``-bucket lines, ``_sum``, and ``_count``.

    *histogram* is expected to have the shape produced by
    ``LatencyHistogram.to_dict()``::

        {
            "buckets": {"500": 3, "1000": 5, ..., "+Inf": 10},
            "count": 10,
            "sum_ms": 12345.67,
        }
    """
    if not histogram or histogram.get("count", 0) == 0:
        return ""

    # Build label prefix for bucket lines (trailing comma) and full label
    # string for _sum/_count lines
    bucket_prefix = ""
    suffix_labels = ""
    if labels:
        parts = [f'{k}="{v}"' for k, v in labels.items()]
        joined = ",".join(parts)
        bucket_prefix = f"{joined},"
        suffix_labels = f"{{{joined}}}"

    lines = [
        f"# HELP {name} {help_text}\n",
        f"# TYPE {name} histogram\n",
    ]

    for le_label, count in histogram.get("buckets", {}).items():
        lines.append(
            f'{name}_bucket{{{bucket_prefix}le="{le_label}"}} {count}\n'
        )

    sum_suffix = f"_sum{suffix_labels}" if suffix_labels else "_sum"
    lines.append(f"{name}{sum_suffix} {histogram.get('sum_ms', 0)}\n")

    count_suffix = f"_count{suffix_labels}" if suffix_labels else "_count"
    lines.append(f"{name}{count_suffix} {histogram.get('count', 0)}\n")

    return "".join(lines)


def _build_prometheus_output(
    token_usage: dict[str, Any],
    snapshot: Any,
    llm_log_dir_bytes: int | None = None,
    db_size_bytes: int | None = None,
    workspace_size_bytes: int | None = None,
    workspace_growth_mb_per_hour: float | None = None,
    disk_free_bytes: int | None = None,
    disk_total_bytes: int | None = None,
    per_chat_tokens: list[dict[str, Any]] | None = None,
) -> str:
    """Build the full Prometheus text exposition from metrics data."""
    lines: list[str] = []

    # ── Token Usage ──────────────────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_token_usage_prompt_total",
            "Total prompt tokens consumed",
            "counter",
            token_usage.get("prompt_tokens", 0),
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_token_usage_completion_total",
            "Total completion tokens consumed",
            "counter",
            token_usage.get("completion_tokens", 0),
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_token_usage_total",
            "Total tokens consumed (prompt + completion)",
            "counter",
            token_usage.get("total_tokens", 0),
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_llm_requests_total",
            "Total LLM API requests made",
            "counter",
            token_usage.get("request_count", 0),
        )
    )

    # ── Per-Chat Token Usage ────────────────────────────────────────────────
    if per_chat_tokens:
        for entry in per_chat_tokens:
            chat_id = _redact_chat_id(entry.get("chat_id", "unknown"))
            lines.append(
                _format_prometheus_metric(
                    "custombot_chat_prompt_tokens",
                    "Per-chat prompt tokens consumed (top chats)",
                    "counter",
                    entry.get("prompt", 0),
                    labels={"chat_id": chat_id},
                )
            )
            lines.append(
                _format_prometheus_metric(
                    "custombot_chat_completion_tokens",
                    "Per-chat completion tokens consumed (top chats)",
                    "counter",
                    entry.get("completion", 0),
                    labels={"chat_id": chat_id},
                )
            )

    # ── Message Metrics ─────────────────────────────────────────────────────
    msg_lat = snapshot.message_latency
    lines.append(
        _format_prometheus_summary(
            "custombot_message_latency_milliseconds",
            "Message processing latency in milliseconds",
            count=msg_lat.count,
            sum_ms=round(msg_lat.mean_ms * msg_lat.count, 2) if msg_lat.count else 0,
            quantiles={
                "0.5": round(msg_lat.median_ms, 2),
                "0.95": round(msg_lat.p95_ms, 2),
                "0.99": round(msg_lat.p99_ms, 2),
            }
            if msg_lat.count
            else None,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_messages_processed_total",
            "Total messages processed",
            "counter",
            snapshot.message_count,
        )
    )

    # ── LLM Latency ─────────────────────────────────────────────────────────
    llm_lat = snapshot.llm_latency
    lines.append(
        _format_prometheus_summary(
            "custombot_llm_latency_milliseconds",
            "LLM API call latency in milliseconds",
            count=llm_lat.count,
            sum_ms=round(llm_lat.mean_ms * llm_lat.count, 2) if llm_lat.count else 0,
            quantiles={
                "0.5": round(llm_lat.median_ms, 2),
                "0.95": round(llm_lat.p95_ms, 2),
                "0.99": round(llm_lat.p99_ms, 2),
            }
            if llm_lat.count
            else None,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_llm_calls_total",
            "Total LLM API calls made",
            "counter",
            snapshot.llm_call_count,
        )
    )

    # ── LLM Error Classification Counter ────────────────────────────────────
    # Prometheus exposition format requires exactly one HELP and one TYPE line
    # per metric name, followed by all label variants.
    if snapshot.llm_error_classifications:
        lines.append("# HELP custombot_llm_errors_total LLM errors classified by error code\n")
        lines.append("# TYPE custombot_llm_errors_total counter\n")
        for code, count in sorted(snapshot.llm_error_classifications.items()):
            safe_code = code.replace('"', '\\"')
            lines.append(f'custombot_llm_errors_total{{code="{safe_code}"}} {count}\n')

    # ── LLM Latency Histogram ──────────────────────────────────────────────
    lines.append(
        _format_prometheus_histogram(
            "custombot_llm_latency",
            "LLM API call latency histogram in milliseconds (fixed buckets)",
            snapshot.llm_latency_histogram,
        )
    )

    # ── ReAct Iteration Metrics ──────────────────────────────────────────────
    react_iters = snapshot.react_iterations
    lines.append(
        _format_prometheus_summary(
            "custombot_react_iterations",
            "Number of ReAct loop iterations per conversation",
            count=react_iters.count,
            sum_ms=round(react_iters.mean_ms * react_iters.count, 2)
            if react_iters.count
            else 0,
            quantiles={
                "0.5": round(react_iters.median_ms, 2),
                "0.95": round(react_iters.p95_ms, 2),
                "0.99": round(react_iters.p99_ms, 2),
            }
            if react_iters.count
            else None,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_react_loop_iterations_total",
            "Cumulative total ReAct loop iterations across all conversations",
            "counter",
            snapshot.react_iterations_total,
        )
    )

    # ── Context Budget Utilization ──────────────────────────────────────────
    if snapshot.context_budget_count > 0:
        lines.append(
            _format_prometheus_metric(
                "custombot_context_budget_utilization_mean",
                "Mean ratio of used tokens to max context budget",
                "gauge",
                round(snapshot.context_budget_mean_ratio, 4),
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_context_budget_utilization_max",
                "Maximum observed ratio of used tokens to max context budget",
                "gauge",
                round(snapshot.context_budget_max_ratio, 4),
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_context_budget_utilization_p95",
                "P95 ratio of used tokens to max context budget",
                "gauge",
                round(snapshot.context_budget_p95_ratio, 4),
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_context_budget_utilization_samples",
                "Number of context-budget utilization samples collected",
                "gauge",
                snapshot.context_budget_count,
            )
        )

    # ── Database Metrics ────────────────────────────────────────────────────
    db_lat = snapshot.db_latency
    lines.append(
        _format_prometheus_summary(
            "custombot_db_latency_milliseconds",
            "Database operation latency in milliseconds",
            count=db_lat.count,
            sum_ms=round(db_lat.mean_ms * db_lat.count, 2) if db_lat.count else 0,
            quantiles={
                "0.5": round(db_lat.median_ms, 2),
                "0.95": round(db_lat.p95_ms, 2),
                "0.99": round(db_lat.p99_ms, 2),
            }
            if db_lat.count
            else None,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_db_operations_total",
            "Total database operations executed",
            "counter",
            snapshot.db_op_count,
        )
    )

    # ── Database Write Latency Metrics ──────────────────────────────────────
    dbw_lat = snapshot.db_write_latency
    lines.append(
        _format_prometheus_summary(
            "custombot_db_write_latency_milliseconds",
            "Database write operation latency in milliseconds",
            count=dbw_lat.count,
            sum_ms=round(dbw_lat.mean_ms * dbw_lat.count, 2) if dbw_lat.count else 0,
            quantiles={
                "0.5": round(dbw_lat.median_ms, 2),
                "0.95": round(dbw_lat.p95_ms, 2),
                "0.99": round(dbw_lat.p99_ms, 2),
            }
            if dbw_lat.count
            else None,
        )
    )
    lines.append(
        _format_prometheus_histogram(
            "custombot_db_write_latency",
            "Database write operation latency histogram in milliseconds (fixed buckets)",
            snapshot.db_write_latency_histogram,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_db_write_operations_total",
            "Total database write operations executed",
            "counter",
            snapshot.db_write_op_count,
        )
    )

    # ── Queue Metrics ────────────────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_queue_depth",
            "Current message queue depth",
            "gauge",
            snapshot.queue_depth,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_queue_max_depth",
            "Maximum observed queue depth",
            "gauge",
            snapshot.queue_max_depth,
        )
    )

    # ── Active Chats ─────────────────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_active_chat_count",
            "Number of currently active chats",
            "gauge",
            snapshot.active_chat_count,
        )
    )

    # ── Memory Cache Metrics ─────────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_memory_cache_hits_total",
            "Total memory cache hits (mtime unchanged, content reused)",
            "counter",
            snapshot.memory_cache_hits,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_memory_cache_misses_total",
            "Total memory cache misses (file changed or not yet cached)",
            "counter",
            snapshot.memory_cache_misses,
        )
    )

    # ── Embedding Cache Metrics ──────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_embed_cache_hits_total",
            "Total embedding cache hits (text already cached, API call avoided)",
            "counter",
            snapshot.embed_cache_hits,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_embed_cache_misses_total",
            "Total embedding cache misses (text not in cache, API call required)",
            "counter",
            snapshot.embed_cache_misses,
        )
    )

    # ── Compression Summary Metrics ──────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_compression_summary_used_total",
            "Total times a compressed conversation summary was used during context assembly",
            "counter",
            snapshot.compression_summary_used_total,
        )
    )

    # ── Skill Metrics ────────────────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_skill_calls_total",
            "Total skill executions",
            "counter",
            snapshot.skill_call_count,
        )
    )
    for skill_name, skill_lat in snapshot.skill_latencies.items():
        lines.append(
            _format_prometheus_summary(
                "custombot_skill_latency_milliseconds",
                "Skill execution latency in milliseconds",
                count=skill_lat.count,
                sum_ms=round(skill_lat.mean_ms * skill_lat.count, 2)
                if skill_lat.count
                else 0,
                quantiles={
                    "0.5": round(skill_lat.median_ms, 2),
                    "0.95": round(skill_lat.p95_ms, 2),
                    "0.99": round(skill_lat.p99_ms, 2),
                }
                if skill_lat.count
                else None,
                labels={"skill": skill_name},
            )
        )
        # Per-skill call count as a labeled metric
        lines.append(
            f'custombot_skill_calls_total{{skill="{skill_name}"}} {skill_lat.count}\n'
        )

    # ── Per-Skill Execution & Error Metrics ──────────────────────────────────
    for skill_name, sm in snapshot.skill_metrics.items():
        # Total executions (success + error)
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_executions_total",
                f"Total executions for {skill_name} (success + error)",
                "counter",
                sm.calls,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_successes_total",
                f"Successful executions for {skill_name}",
                "counter",
                sm.successes,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_errors_total",
                f"Failed executions for {skill_name}",
                "counter",
                sm.errors,
                labels={"skill": skill_name},
            )
        )
        for err_type, count in sm.error_types.items():
            safe_err = err_type.replace('"', '\\"')
            lines.append(
                _format_prometheus_metric(
                    "custombot_skill_errors_total",
                    f"Failed executions for {skill_name} by error type",
                    "counter",
                    count,
                    labels={"skill": skill_name, "error_type": safe_err},
                )
            )
        # Error rate gauge (errors / total executions)
        if sm.calls > 0:
            lines.append(
                _format_prometheus_metric(
                    "custombot_skill_error_rate",
                    f"Error rate for {skill_name} (errors / executions)",
                    "gauge",
                    round(sm.errors / sm.calls, 4),
                    labels={"skill": skill_name},
                )
            )

    # ── Per-Skill Timeout Ratio ──────────────────────────────────────────────
    for skill_name, tr in snapshot.skill_timeout_ratios.items():
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_timeout_ratio_mean",
                "Mean ratio of actual execution time to declared skill timeout",
                "gauge",
                round(tr.mean_ratio, 4),
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_timeout_ratio_max",
                "Maximum observed ratio of actual time to declared skill timeout",
                "gauge",
                round(tr.max_ratio, 4),
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_timeout_ratio_p95",
                "P95 ratio of actual execution time to declared skill timeout",
                "gauge",
                round(tr.p95_ratio, 4),
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_timeout_ratio_samples",
                "Number of timeout-ratio samples collected per skill",
                "gauge",
                tr.count,
                labels={"skill": skill_name},
            )
        )

    # ── Per-Skill Oversized Argument Rejections ──────────────────────────────
    for skill_name, count in snapshot.skill_oversized_args.items():
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_args_oversized_total",
                f"Number of rejected calls for {skill_name} due to oversized arguments",
                "counter",
                count,
                labels={"skill": skill_name},
            )
        )

    # ── Per-Skill Oversized Argument Size Distribution ────────────────────────
    for skill_name, stats in snapshot.skill_oversized_args_sizes.items():
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_args_oversized_min_bytes",
                f"Smallest oversized argument payload size for {skill_name}",
                "gauge",
                stats.min_bytes,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_args_oversized_max_bytes",
                f"Largest oversized argument payload size for {skill_name}",
                "gauge",
                stats.max_bytes,
                labels={"skill": skill_name},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_skill_args_oversized_total_bytes",
                f"Cumulative oversized argument payload size for {skill_name}",
                "counter",
                stats.total_bytes,
                labels={"skill": skill_name},
            )
        )

    # ── Per-Chat Message Counts ──────────────────────────────────────────────
    for chat_metric in snapshot.top_chats:
        lines.append(
            _format_prometheus_metric(
                "custombot_chat_messages_total",
                "Per-chat message count (top chats)",
                "counter",
                chat_metric.message_count,
                labels={"chat_id": _redact_chat_id(chat_metric.chat_id)},
            )
        )

    # ── Per-Chat Conversation Depth ──────────────────────────────────────────
    for depth_entry in snapshot.top_chat_depths:
        lines.append(
            _format_prometheus_metric(
                "custombot_chat_conversation_depth",
                "Last ReAct iteration count per chat (top chats by depth)",
                "gauge",
                depth_entry.depth,
                labels={"chat_id": _redact_chat_id(depth_entry.chat_id)},
            )
        )

    # ── System Metrics ───────────────────────────────────────────────────────
    if snapshot.cpu_percent > 0:
        lines.append(
            _format_prometheus_metric(
                "custombot_cpu_percent",
                "CPU usage percentage",
                "gauge",
                round(snapshot.cpu_percent, 1),
            )
        )
    if snapshot.memory_percent > 0:
        lines.append(
            _format_prometheus_metric(
                "custombot_memory_percent",
                "Memory usage percentage",
                "gauge",
                round(snapshot.memory_percent, 1),
            )
        )

    # ── Error Rate Trends ────────────────────────────────────────────────────
    lines.append(
        _format_prometheus_metric(
            "custombot_errors_total",
            "Total errors recorded since startup",
            "counter",
            snapshot.total_error_count,
        )
    )
    for ew in snapshot.error_windows:
        window_label = f"{ew.window_seconds // 60}m"
        lines.append(
            _format_prometheus_metric(
                "custombot_error_rate",
                f"Errors in the last {window_label}",
                "gauge",
                ew.error_count,
                labels={"window": window_label},
            )
        )
        lines.append(
            _format_prometheus_metric(
                "custombot_error_rate_per_minute",
                f"Average errors per minute over the last {window_label}",
                "gauge",
                round(ew.error_rate_per_minute, 4),
                labels={"window": window_label},
            )
        )

    # ── LLM Log Directory Size ──────────────────────────────────────────────
    if llm_log_dir_bytes is not None:
        lines.append(
            _format_prometheus_metric(
                "custombot_llm_log_dir_bytes",
                "Total size of LLM request/response log directory in bytes",
                "gauge",
                llm_log_dir_bytes,
            )
        )

    # ── Disk Usage ──────────────────────────────────────────────────────────
    if db_size_bytes is not None:
        lines.append(
            _format_prometheus_metric(
                "custombot_db_size_bytes",
                "Total size of database directory (workspace/.data/) in bytes",
                "gauge",
                db_size_bytes,
            )
        )
    if workspace_size_bytes is not None:
        lines.append(
            _format_prometheus_metric(
                "custombot_workspace_size_bytes",
                "Total size of workspace directory in bytes",
                "gauge",
                workspace_size_bytes,
            )
        )
    if workspace_growth_mb_per_hour is not None:
        lines.append(
            _format_prometheus_metric(
                "custombot_workspace_growth_mb_per_hour",
                "Workspace disk usage growth rate in MB per hour",
                "gauge",
                round(workspace_growth_mb_per_hour, 3),
            )
        )

    # ── Filesystem Disk Space ──────────────────────────────────────────────
    if disk_free_bytes is not None:
        lines.append(
            _format_prometheus_metric(
                "custombot_disk_free_bytes",
                "Available disk space on the workspace partition in bytes",
                "gauge",
                disk_free_bytes,
            )
        )
    if disk_total_bytes is not None:
        lines.append(
            _format_prometheus_metric(
                "custombot_disk_total_bytes",
                "Total disk capacity on the workspace partition in bytes",
                "gauge",
                disk_total_bytes,
            )
        )

    return "".join(lines)


def _build_scheduler_prometheus_output(scheduler: Any) -> str:
    """Build Prometheus metrics for the task scheduler."""
    if scheduler is None:
        return ""

    status = scheduler.get_status()
    lines: list[str] = []

    running = 1 if status["running"] else 0
    lines.append(
        _format_prometheus_metric(
            "custombot_scheduler_running",
            "Whether the task scheduler is running (1=yes, 0=no)",
            "gauge",
            running,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_scheduler_tasks_total",
            "Total number of scheduled tasks",
            "gauge",
            status["total_tasks"],
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_scheduler_enabled_tasks",
            "Number of enabled scheduled tasks",
            "gauge",
            status["enabled_tasks"],
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_scheduler_chats_with_tasks",
            "Number of chats with at least one scheduled task",
            "gauge",
            status["chats_with_tasks"],
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_scheduler_successes_total",
            "Total successful scheduled task executions",
            "counter",
            status["success_count"],
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_scheduler_failures_total",
            "Total failed scheduled task executions",
            "counter",
            status["failure_count"],
        )
    )

    return "".join(lines)


def _build_circuit_breaker_prometheus_output(circuit_breaker: Any) -> str:
    """Build Prometheus metrics for the LLM circuit breaker."""
    if circuit_breaker is None:
        return ""

    from src.utils.circuit_breaker import CircuitState

    state = circuit_breaker.state
    state_value = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 1,
        CircuitState.OPEN: 2,
    }.get(state, 0)

    lines: list[str] = []
    lines.append(
        _format_prometheus_metric(
            "custombot_llm_circuit_breaker_state",
            "Circuit breaker state (0=closed, 1=half-open, 2=open)",
            "gauge",
            state_value,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_llm_circuit_breaker_failures_total",
            "Total consecutive LLM failures recorded by the circuit breaker",
            "counter",
            circuit_breaker.failure_count,
        )
    )
    return "".join(lines)


def _build_db_write_breaker_prometheus_output(circuit_breaker: Any) -> str:
    """Build Prometheus metrics for the database write circuit breaker."""
    if circuit_breaker is None:
        return ""

    from src.utils.circuit_breaker import CircuitState

    state = circuit_breaker.state
    state_value = {
        CircuitState.CLOSED: 0,
        CircuitState.HALF_OPEN: 1,
        CircuitState.OPEN: 2,
    }.get(state, 0)

    lines: list[str] = []
    lines.append(
        _format_prometheus_metric(
            "custombot_db_write_circuit_breaker_state",
            "DB write circuit breaker state (0=closed, 1=half-open, 2=open)",
            "gauge",
            state_value,
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_db_write_circuit_breaker_failures_total",
            "Consecutive DB write failures recorded by the circuit breaker",
            "counter",
            circuit_breaker.failure_count,
        )
    )
    return "".join(lines)


def _build_dedup_prometheus_output(dedup_stats: Any) -> str:
    """Build Prometheus metrics for the unified dedup service."""
    if dedup_stats is None:
        return ""

    stats = dedup_stats.to_dict()
    lines: list[str] = []

    lines.append(
        _format_prometheus_metric(
            "custombot_dedup_inbound_hits_total",
            "Number of duplicate inbound messages detected by message-id dedup",
            "counter",
            stats.get("inbound_hits", 0),
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_dedup_inbound_misses_total",
            "Number of unique inbound messages passed by message-id dedup",
            "counter",
            stats.get("inbound_misses", 0),
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_dedup_outbound_hits_total",
            "Number of duplicate outbound messages suppressed by content-hash dedup",
            "counter",
            stats.get("outbound_hits", 0),
        )
    )
    lines.append(
        _format_prometheus_metric(
            "custombot_dedup_outbound_misses_total",
            "Number of unique outbound messages delivered (content-hash dedup)",
            "counter",
            stats.get("outbound_misses", 0),
        )
    )
    return "".join(lines)


def _build_event_bus_prometheus_output(event_bus: Any) -> str:
    """Build Prometheus metrics for EventBus emission and handler counts."""
    if event_bus is None:
        return ""

    metrics = event_bus.get_metrics()
    lines: list[str] = []

    for event_name, count in sorted(metrics["emissions"].items()):
        lines.append(
            _format_prometheus_metric(
                "custombot_event_emitted_total",
                "Total number of EventBus emissions per event name",
                "counter",
                count,
                labels={"event": event_name},
            )
        )

    for event_name, count in sorted(metrics["invocations"].items()):
        lines.append(
            _format_prometheus_metric(
                "custombot_event_handler_invocations_total",
                "Total number of handler invocations per event name",
                "counter",
                count,
                labels={"event": event_name},
            )
        )

    return "".join(lines)


class HealthServer:
    """HTTP health check server using aiohttp."""

    def __init__(
        self,
        db: Optional["Database"] = None,
        neonize_backend: Optional["NeonizeBackend"] = None,
        llm_api_key: Optional[str] = None,
        llm_base_url: Optional[str] = None,
        check_whatsapp: bool = True,
        check_llm: bool = False,
        check_memory: bool = True,
        check_performance: bool = True,
        include_token_usage: bool = True,
        token_usage: Any = None,
        bot: Optional["Bot"] = None,
        scheduler: Optional["TaskScheduler"] = None,
        llm_log_dir: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        shutdown_mgr: Optional["GracefulShutdown"] = None,
        startup_durations: Optional[dict[str, float]] = None,
    ) -> None:
        self._db = db
        self._neonize_backend = neonize_backend
        self._llm_api_key = llm_api_key
        self._llm_base_url = llm_base_url or "https://api.openai.com/v1"
        self._check_whatsapp = check_whatsapp
        self._check_llm = check_llm
        self._check_memory = check_memory
        self._check_performance = check_performance
        self._include_token_usage = include_token_usage
        self._token_usage = token_usage
        self._bot = bot
        self._scheduler = scheduler
        self._llm_log_dir = llm_log_dir
        self._workspace_dir = workspace_dir
        self._shutdown_mgr = shutdown_mgr
        self._startup_durations = startup_durations
        self._startup_total_seconds: Optional[float] = None
        self._runner: Optional[Any] = None
        self._site: Optional[Any] = None
        self._port: int = 8080

    def update_startup_durations(self, durations: dict[str, float]) -> None:
        """Replace the startup-durations snapshot with the final, complete data.

        Called once after all startup steps finish so that ``/health`` returns
        timing for *every* component — not just the steps that happened to run
        before the Health Server was created.
        """
        self._startup_durations = durations
        self._startup_total_seconds = sum(durations.values())

    async def _get_health_report(self) -> HealthReport:
        """Run all health checks and return a report."""
        components: list[ComponentHealth] = []

        if self._db:
            components.append(await check_database(self._db))
        else:
            components.append(
                ComponentHealth(
                    name="database",
                    status=HealthStatus.UNHEALTHY,
                    message="Database not configured",
                )
            )

        if self._check_whatsapp:
            components.append(await check_neonize(self._neonize_backend))

        if self._check_llm and self._llm_api_key:
            components.append(await check_llm_credentials(self._llm_api_key, self._llm_base_url))

        if self._check_memory:
            try:
                from src.monitoring import check_memory_health

                memory_result = await check_memory_health()
                if "component" in memory_result:
                    components.append(memory_result["component"])
            except ImportError:
                components.append(
                    ComponentHealth(
                        name="memory",
                        status=HealthStatus.DEGRADED,
                        message="psutil not installed",
                    )
                )
            except Exception as e:
                log.debug("Memory health check error: %s", e)
                components.append(
                    ComponentHealth(
                        name="memory",
                        status=HealthStatus.DEGRADED,
                        message=f"Memory check error: {type(e).__name__}",
                    )
                )

        if self._check_performance:
            try:
                from src.monitoring import check_performance_health

                perf_result = await check_performance_health()
                if "component" in perf_result:
                    components.append(perf_result["component"])
            except ImportError:
                components.append(
                    ComponentHealth(
                        name="performance",
                        status=HealthStatus.DEGRADED,
                        message="Performance metrics not available",
                    )
                )
            except Exception as e:
                log.debug("Performance health check error: %s", e)
                components.append(
                    ComponentHealth(
                        name="performance",
                        status=HealthStatus.DEGRADED,
                        message=f"Performance check error: {type(e).__name__}",
                    )
                )

        # Wiring validation (startup component wiring)
        if self._bot is not None:
            try:
                wiring_result = self._bot.validate_wiring()
                components.append(check_wiring(wiring_result))
            except Exception as e:
                log.debug("Wiring health check error: %s", e)
                components.append(
                    ComponentHealth(
                        name="wiring",
                        status=HealthStatus.UNHEALTHY,
                        message=f"Wiring check failed: {type(e).__name__}",
                    )
                )

        # Scheduler status
        try:
            components.append(check_scheduler(self._scheduler))
        except Exception as e:
            log.debug("Scheduler health check error: %s", e)
            components.append(
                ComponentHealth(
                    name="scheduler",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Scheduler check failed: {type(e).__name__}",
                )
            )

        # LLM log directory status
        try:
            components.append(check_llm_logs(self._llm_log_dir))
        except Exception as e:
            log.debug("LLM logs health check error: %s", e)
            components.append(
                ComponentHealth(
                    name="llm_logs",
                    status=HealthStatus.DEGRADED,
                    message=f"LLM logs check failed: {type(e).__name__}",
                )
            )

        # Disk usage for database and workspace directories
        if self._workspace_dir:
            try:
                components.append(check_disk_usage(self._workspace_dir))
            except Exception as e:
                log.debug("Disk usage health check error: %s", e)
                components.append(
                    ComponentHealth(
                        name="disk_usage",
                        status=HealthStatus.DEGRADED,
                        message=f"Disk usage check failed: {type(e).__name__}",
                    )
                )

            # Filesystem-level free disk space check
            try:
                components.append(check_disk_space_health(self._workspace_dir))
            except Exception as e:
                log.debug("Disk space health check error: %s", e)
                components.append(
                    ComponentHealth(
                        name="disk_space",
                        status=HealthStatus.DEGRADED,
                        message=f"Disk space check failed: {type(e).__name__}",
                    )
                )

            # Workspace monitor cleanup stats
            try:
                from src.monitoring.workspace_monitor import check_workspace_health

                ws_result = await check_workspace_health(self._workspace_dir)
                if "component" in ws_result:
                    components.append(ws_result["component"])
            except Exception as e:
                log.debug("Workspace health check error: %s", e)

        token_usage = None
        if self._include_token_usage:
            token_usage = get_token_usage_stats(self._token_usage)

        return HealthReport(
            components=components,
            token_usage=token_usage,
            startup_durations=self._startup_durations,
            startup_total_seconds=self._startup_total_seconds,
        )

    async def _handle_health(self, request: Any) -> Any:
        """Handle GET /health requests.

        Returns HTTP 200 for HEALTHY, HTTP 200 with X-Health-Status header
        for DEGRADED (so monitoring tools can detect it), and HTTP 503 for
        UNHEALTHY.
        """
        from aiohttp import web

        report = await self._get_health_report()
        if report.status == HealthStatus.UNHEALTHY:
            status_code = 503
        else:
            status_code = 200

        response = web.json_response(report.to_dict(), status=status_code)
        if report.status == HealthStatus.DEGRADED:
            response.headers["X-Health-Status"] = "degraded"
        return response

    async def _handle_root(self, request: Any) -> Any:
        """Handle GET / requests with basic info."""
        from aiohttp import web

        return web.json_response(
            {
                "name": "custombot",
                "message": (
                    "Bot is running. Use /health for health check, "
                    "/metrics for Prometheus metrics, /version for version info."
                ),
            }
        )

    async def _handle_ready(self, request: Any) -> Any:
        """Handle GET /ready — Kubernetes-style readiness probe.

        Returns HTTP 200 only when all components (including the WhatsApp
        channel) are fully initialized and the bot is accepting messages.
        Returns HTTP 503 otherwise, listing the reasons the bot is not ready.
        """
        from aiohttp import web

        from src.health.checks import check_readiness

        ready, reasons = check_readiness(
            shutdown_accepting=(
                self._shutdown_mgr.accepting_messages
                if self._shutdown_mgr is not None
                else False
            ),
            neonize_backend=self._neonize_backend,
            bot_wired=self._bot is not None,
            db_available=self._db is not None,
        )

        body: dict[str, Any] = {"ready": ready}
        if reasons:
            body["reasons"] = reasons

        # Include WhatsApp connection status for headless deployment monitoring
        if self._neonize_backend is not None:
            body["whatsapp"] = {
                "connected": self._neonize_backend.is_connected,
                "ready": self._neonize_backend.is_ready,
            }
            if self._neonize_backend.is_connected:
                body["whatsapp"]["status"] = "connected"
            else:
                body["whatsapp"]["status"] = (
                    "waiting-for-qr" if not self._neonize_backend.is_ready
                    else "disconnected"
                )

        return web.json_response(body, status=200 if ready else 503)

    async def _handle_version(self, request: Any) -> Any:
        """Handle GET /version — return bot version and Python runtime info."""
        import platform

        from aiohttp import web

        from src.__version__ import __version__

        return web.json_response(
            {
                "version": __version__,
                "python": platform.python_version(),
            }
        )

    async def _handle_metrics(self, request: Any) -> Any:
        """Handle GET /metrics requests in Prometheus text exposition format."""
        from aiohttp import web

        try:
            token_usage = get_token_usage_stats(self._token_usage)
            from src.monitoring.performance import get_metrics_collector

            metrics = get_metrics_collector()
            await metrics.refresh_system_metrics()
            snapshot = metrics.get_snapshot(include_system=True)

            # Collect LLM log directory size
            llm_log_bytes: int | None = None
            if self._llm_log_dir:
                from src.logging.llm_logging import _dir_size
                from pathlib import Path

                llm_log_bytes = _dir_size(Path(self._llm_log_dir))

            # Collect disk usage for database and workspace
            db_size_bytes: int | None = None
            workspace_size_bytes: int | None = None
            workspace_growth: float | None = None
            disk_free_bytes: int | None = None
            disk_total_bytes: int | None = None
            if self._workspace_dir:
                from pathlib import Path

                from src.health.checks import _recursive_dir_size

                ws = Path(self._workspace_dir)
                data_dir = ws / ".data"
                db_size_bytes = _recursive_dir_size(data_dir) if data_dir.exists() else 0
                workspace_size_bytes = _recursive_dir_size(ws)

                # Growth rate from WorkspaceMonitor's accumulated samples
                try:
                    from src.monitoring.workspace_monitor import get_global_workspace_monitor

                    monitor = get_global_workspace_monitor(
                        workspace_dir=self._workspace_dir,
                    )
                    last = monitor.last_stats
                    if last is not None and last.growth_mb_per_hour is not None:
                        workspace_growth = last.growth_mb_per_hour
                except Exception:
                    pass

                # Filesystem-level free/total via existing disk utility
                try:
                    from src.utils.disk import check_disk_space

                    ds_result = check_disk_space(ws)
                    disk_free_bytes = ds_result.free_bytes
                    disk_total_bytes = ds_result.total_bytes
                except OSError:
                    pass

            # Per-chat token metrics (if token_usage object has get_top_chats)
            per_chat = None
            if self._token_usage and hasattr(self._token_usage, "get_top_chats"):
                per_chat = self._token_usage.get_top_chats()

            output = _build_prometheus_output(
                token_usage, snapshot, llm_log_bytes, db_size_bytes,
                workspace_size_bytes, workspace_growth,
                disk_free_bytes, disk_total_bytes,
                per_chat_tokens=per_chat,
            )
            output += _build_scheduler_prometheus_output(self._scheduler)
            # Circuit breaker metrics (via public Bot accessor)
            cb = self._bot.get_llm_status() if self._bot is not None else None
            output += _build_circuit_breaker_prometheus_output(cb)
            # DB write circuit breaker metrics
            db_cb = self._bot.get_db_write_breaker() if self._bot is not None else None
            output += _build_db_write_breaker_prometheus_output(db_cb)
            # Dedup service metrics (via public Bot accessor)
            dedup_stats = self._bot.get_dedup_stats() if self._bot is not None else None
            output += _build_dedup_prometheus_output(dedup_stats)
            # EventBus emission and handler metrics
            try:
                from src.core.event_bus import get_event_bus
                output += _build_event_bus_prometheus_output(get_event_bus())
            except Exception:
                pass
            return web.Response(
                text=output,
                content_type="text/plain",
                charset="utf-8",
            )
        except Exception as e:
            log.error("Metrics endpoint error: %s", e, exc_info=True)
            return web.Response(
                text=f"# Error generating metrics: {type(e).__name__}\n",
                status=500,
                content_type="text/plain",
                charset="utf-8",
            )

    @staticmethod
    async def _add_security_headers(request: Any, response: Any) -> None:
        """Inject security headers into every response.

        Defense-in-depth headers prevent content-type sniffing, clickjacking,
        framing, and caching of sensitive metrics data even on internal endpoints.
        """
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"

    async def start(self, port: int = 8080, host: str = "127.0.0.1") -> None:
        """Start the health check HTTP server.

        Default binds to localhost only to prevent exposing internal state
        (DB status, token counts, connection info) to the network.
        Set host="0.0.0.0" to expose to all interfaces.
        """
        from aiohttp import web

        self._port = port

        # Build middleware stack
        middlewares: list[Any] = []

        # Method validation (applied first — cheapest check)
        middlewares.append(_create_method_validation_middleware())

        # Request size limits
        max_body, max_url = _load_request_size_config()
        middlewares.append(
            _create_request_size_limit_middleware(max_body, max_url)
        )

        # Per-IP rate limiting middleware
        limit, window, max_ips = _load_rate_limit_config()
        ip_limiter = _IPLimiter(limit, window, max_ips)
        middlewares.append(_create_rate_limit_middleware(ip_limiter))

        # Optional HMAC authentication middleware
        hmac_secret = _load_hmac_secret()
        if hmac_secret:
            middlewares.append(_create_hmac_middleware(hmac_secret))

        app = web.Application(middlewares=middlewares)
        app.on_response_prepare.append(self._add_security_headers)
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/ready", self._handle_ready)
        app.router.add_get("/version", self._handle_version)
        app.router.add_get("/metrics", self._handle_metrics)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()

        auth_status = "HMAC enabled" if hmac_secret else "no auth"
        log.info(
            "Health check server started on http://%s:%d (%s, rate limit: %d req/%ds per IP, max body: %dB, max URL: %d chars)",
            host,
            port,
            auth_status,
            limit,
            int(window),
            max_body,
            max_url,
        )

        # Defense-in-depth: install a log filter that redacts any HMAC
        # credential tokens from log output.  Applied to the module
        # logger *and* aiohttp's internal loggers so that DEBUG-level
        # access logging never leaks the raw Authorization header.
        if hmac_secret:
            _redacting = _SecretRedactingFilter(secret=hmac_secret)
            log.addFilter(_redacting)
            for _logger_name in ("aiohttp.access", "aiohttp.server"):
                logging.getLogger(_logger_name).addFilter(_redacting)

    async def stop(self) -> None:
        """Stop the health check HTTP server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            log.info("Health check server stopped")

    @property
    def port(self) -> int:
        """Get the port the server is listening on."""
        return self._port


async def run_health_server(
    db: Optional["Database"] = None,
    neonize_backend: Optional["NeonizeBackend"] = None,
    port: int = 8080,
    check_whatsapp: bool = True,
    token_usage: Any = None,
) -> HealthServer:
    """Create and start a health server. Convenience function for quick setup."""
    server = HealthServer(
        db=db,
        neonize_backend=neonize_backend,
        check_whatsapp=check_whatsapp,
        token_usage=token_usage,
    )
    await server.start(port=port)
    return server
