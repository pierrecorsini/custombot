"""
src/health/middleware.py — HTTP middleware for the health check server.

Provides middleware functions for:
- Per-IP rate limiting with sliding window tracking
- HTTP method validation (read-only enforcement)
- Request body and URL size limits
- Request path whitelisting (reject unknown paths early)
- Optional HMAC-SHA256 authentication

Also includes the ``SecretRedactingFilter`` log filter that scrubs HMAC
credential tokens from log output as a defense-in-depth measure.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import time
from typing import Any

from src.rate_limiter import SlidingWindowTracker
from src.utils import BoundedOrderedDict
from src.utils.locking import ThreadLock

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Per-IP Rate Limiting for Health Server
# ─────────────────────────────────────────────────────────────────────────────


def load_rate_limit_config() -> tuple[int, float, int]:
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


class IPLimiter:
    """Per-IP sliding window rate limiter with LRU eviction."""

    def __init__(self, limit: int, window_seconds: float, max_ips: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._trackers: BoundedOrderedDict[str, SlidingWindowTracker] = (
            BoundedOrderedDict(max_size=max_ips, eviction="half")
        )
        self._lock = ThreadLock()

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


def create_rate_limit_middleware(limiter: IPLimiter) -> Any:
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


def create_method_validation_middleware() -> Any:
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


# ─────────────────────────────────────────────────────────────────────────────
# Request Path Validation
# ─────────────────────────────────────────────────────────────────────────────


def create_path_validation_middleware(allowed_paths: frozenset[str]) -> Any:
    """Create an aiohttp middleware that rejects requests to unknown paths.

    Health endpoints serve a fixed set of routes.  Rejecting requests to
    arbitrary paths early — before rate-limit counting, HMAC verification, or
    handler logic — prevents cache-poisoning, log noise, and wasted processing
    from URL probes.
    """
    from aiohttp import web

    @web.middleware
    async def path_validation_middleware(
        request: web.Request, handler: Any
    ) -> Any:
        if request.path not in allowed_paths:
            log.warning(
                "Health server rejected unknown path: %s", request.path
            )
            return web.Response(
                text="Not Found",
                status=404,
                content_type="text/plain",
            )
        return await handler(request)

    return path_validation_middleware


def load_request_size_config() -> tuple[int, int]:
    """Load health HTTP request size limits from env or defaults."""
    from src.constants import HEALTH_MAX_REQUEST_BODY_BYTES, HEALTH_MAX_URL_LENGTH

    body_str = os.environ.get("HEALTH_MAX_BODY_BYTES", "")
    url_str = os.environ.get("HEALTH_MAX_URL_LENGTH", "")

    return (
        int(body_str) if body_str.isdigit() else HEALTH_MAX_REQUEST_BODY_BYTES,
        int(url_str) if url_str.isdigit() else HEALTH_MAX_URL_LENGTH,
    )


def create_request_size_limit_middleware(
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


def load_hmac_secret() -> str | None:
    """Load the optional HMAC secret from the environment."""
    secret = os.environ.get("HEALTH_HMAC_SECRET", "").strip()
    return secret if secret else None


def mask_hmac_header(value: str) -> str:
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


class SecretRedactingFilter(logging.Filter):
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


def verify_hmac(request: Any, secret: str) -> bool:
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


def create_hmac_middleware(secret: str) -> Any:
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
        authenticated = auth_value is not None and verify_hmac(request, secret)

        # Mask the Authorization header in-place so downstream logging,
        # access-log middleware, and error handlers never see the raw
        # HMAC token in full.
        if auth_value is not None:
            request.headers["Authorization"] = mask_hmac_header(auth_value)

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
