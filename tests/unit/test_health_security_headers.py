"""
Tests for src/health/server.py — security headers and IP rate limiting.

Verifies that every HTTP response from the health server includes the
required defense-in-depth headers:
- X-Content-Type-Options: nosniff
- Content-Security-Policy: default-src 'none'
- X-Frame-Options: DENY
- Cache-Control: no-store
- Strict-Transport-Security (only when request is HTTPS or behind a
  TLS-terminating proxy with X-Forwarded-Proto: https)

Also verifies ``IPLimiter`` behaviour:
- Requests within the limit are allowed.
- Requests exceeding the limit are rejected with ``retry_after``.
- After the sliding window expires, requests are allowed again.
- LRU eviction works when ``max_ips`` is exceeded.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.health.middleware import (
    IPLimiter,
    create_path_validation_middleware,
    create_rate_limit_middleware,
    create_request_size_limit_middleware,
    load_rate_limit_config,
    load_request_size_config,
)
from src.health.server import HealthServer


# ── Helpers ──────────────────────────────────────────────────────────────

_REQUIRED_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "default-src 'none'",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}

_ROUTES = ["/", "/health", "/ready", "/version"]


async def _create_test_client(server: HealthServer) -> Any:
    """Build an aiohttp test client around the HealthServer's app."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from src.constants import HEALTH_ALLOWED_PATHS

    max_body, max_url = load_request_size_config()
    limit, window, max_ips = load_rate_limit_config()
    ip_limiter = IPLimiter(limit, window, max_ips)

    middlewares = [
        create_path_validation_middleware(HEALTH_ALLOWED_PATHS),
        create_request_size_limit_middleware(max_body, max_url),
        create_rate_limit_middleware(ip_limiter),
    ]

    app = web.Application(middlewares=middlewares)
    app.on_response_prepare.append(server._add_security_headers)
    app.router.add_get("/", server._handle_root)
    app.router.add_get("/health", server._handle_health)
    app.router.add_get("/ready", server._handle_ready)
    app.router.add_get("/version", server._handle_version)
    app.router.add_get("/metrics", server._handle_metrics)

    test_server = TestServer(app)
    client = TestClient(test_server)
    await client.start_server()
    return client


def _make_server() -> HealthServer:
    """Build a minimal HealthServer with all optional deps mocked."""
    bot = MagicMock()
    bot.get_llm_status.return_value = None
    bot.get_db_write_breaker.return_value = None
    bot.get_dedup_stats.return_value = None

    return HealthServer(
        bot=bot,
        check_whatsapp=False,
        check_llm=False,
        check_memory=False,
        check_performance=False,
    )


# ── Tests ────────────────────────────────────────────────────────────────


class TestSecurityHeaders:
    """Every response must carry the full set of security headers."""

    @pytest.mark.parametrize("route", _ROUTES)
    async def test_security_headers_present(self, route: str) -> None:
        client = await _create_test_client(_make_server())
        try:
            resp = await client.get(route)
            for header, expected in _REQUIRED_SECURITY_HEADERS.items():
                actual = resp.headers.get(header)
                assert actual == expected, (
                    f"{header}: expected {expected!r}, got {actual!r} on {route}"
                )
        finally:
            await client.close()

    async def test_hsts_header_absent_over_plain_http(self) -> None:
        """HSTS header must NOT be present when request is plain HTTP."""
        client = await _create_test_client(_make_server())
        try:
            resp = await client.get("/version")
            assert "Strict-Transport-Security" not in resp.headers
        finally:
            await client.close()

    async def test_hsts_header_present_with_forwarded_proto_https(self) -> None:
        """HSTS header is added when X-Forwarded-Proto: https is set."""
        client = await _create_test_client(_make_server())
        try:
            resp = await client.get(
                "/version", headers={"X-Forwarded-Proto": "https"}
            )
            hsts = resp.headers.get("Strict-Transport-Security")
            assert hsts is not None, "HSTS header should be present over HTTPS proxy"
            assert "max-age=" in hsts
            assert "includeSubDomains" in hsts
        finally:
            await client.close()

    async def test_hsts_header_absent_with_forwarded_proto_http(self) -> None:
        """HSTS header must NOT be present when X-Forwarded-Proto is http."""
        client = await _create_test_client(_make_server())
        try:
            resp = await client.get(
                "/version", headers={"X-Forwarded-Proto": "http"}
            )
            assert "Strict-Transport-Security" not in resp.headers
        finally:
            await client.close()


class TestIPLimiter:
    """Unit tests for ``IPLimiter`` rate limiting, burst, cooldown, and eviction."""

    def _make_limiter(
        self, limit: int = 3, window: float = 5.0, max_ips: int = 4
    ) -> IPLimiter:
        return IPLimiter(limit, window, max_ips)

    # ── (a) Requests within the limit are allowed ────────────────────────

    def test_requests_within_limit_are_allowed(self) -> None:
        limiter = self._make_limiter(limit=3, window=5.0)

        for i in range(3):
            allowed, remaining, retry_after = limiter.check("1.2.3.4")
            assert allowed, f"Request {i + 1}/3 should be allowed"
            assert retry_after == 0.0

    def test_remaining_decrements_on_each_request(self) -> None:
        limiter = self._make_limiter(limit=3, window=5.0)

        _, remaining_1, _ = limiter.check("1.2.3.4")
        _, remaining_2, _ = limiter.check("1.2.3.4")
        _, remaining_3, _ = limiter.check("1.2.3.4")

        assert remaining_1 > remaining_2 > remaining_3

    def test_different_ips_tracked_independently(self) -> None:
        limiter = self._make_limiter(limit=2, window=5.0)

        # Exhaust limit for IP A
        limiter.check("1.1.1.1")
        limiter.check("1.1.1.1")

        # IP B should still be allowed
        allowed, _, _ = limiter.check("2.2.2.2")
        assert allowed

    # ── (b) Requests exceeding the limit are rejected with retry_after ───

    def test_request_exceeding_limit_is_rejected(self) -> None:
        limiter = self._make_limiter(limit=2, window=5.0)

        limiter.check("1.2.3.4")
        limiter.check("1.2.3.4")

        allowed, remaining, retry_after = limiter.check("1.2.3.4")
        assert not allowed
        assert remaining == 0
        assert retry_after > 0.0

    def test_retry_after_is_within_window(self) -> None:
        window = 5.0
        limiter = self._make_limiter(limit=2, window=window)

        limiter.check("1.2.3.4")
        limiter.check("1.2.3.4")

        _, _, retry_after = limiter.check("1.2.3.4")
        assert 0.0 < retry_after <= window

    def test_subsequent_rejections_still_return_retry_after(self) -> None:
        limiter = self._make_limiter(limit=1, window=5.0)

        limiter.check("1.2.3.4")

        # Two consecutive rejections should both return retry_after
        _, _, r1 = limiter.check("1.2.3.4")
        _, _, r2 = limiter.check("1.2.3.4")
        assert r1 > 0.0
        assert r2 > 0.0

    # ── (c) After the window expires, requests are allowed again ─────────

    def test_requests_allowed_after_window_expires(self) -> None:
        window = 2.0
        limiter = self._make_limiter(limit=2, window=window)

        base = time.time()
        with patch("time.time", return_value=base):
            limiter.check("1.2.3.4")
            limiter.check("1.2.3.4")
            # Exhausted — third request should be denied
            allowed, _, _ = limiter.check("1.2.3.4")
            assert not allowed

        # Advance time past the window
        with patch("time.time", return_value=base + window + 0.1):
            allowed, remaining, retry_after = limiter.check("1.2.3.4")
            assert allowed
            assert retry_after == 0.0

    def test_partial_window_expiry_allows_more_requests(self) -> None:
        window = 4.0
        limiter = self._make_limiter(limit=3, window=window)

        base = time.time()
        with patch("time.time", return_value=base):
            limiter.check("1.2.3.4")  # slot 1

        # Second request 2s later
        with patch("time.time", return_value=base + 2.0):
            limiter.check("1.2.3.4")  # slot 2

        # Third request 3s later
        with patch("time.time", return_value=base + 3.0):
            limiter.check("1.2.3.4")  # slot 3 — limit reached

        # At t=3.1, still exhausted
        with patch("time.time", return_value=base + 3.1):
            allowed, _, _ = limiter.check("1.2.3.4")
            assert not allowed

        # At t=4.1, first slot (at t=0) has expired → one slot free
        with patch("time.time", return_value=base + 4.1):
            allowed, _, _ = limiter.check("1.2.3.4")
            assert allowed

    # ── (d) LRU eviction works when max_ips is exceeded ──────────────────

    def test_lru_eviction_when_max_ips_exceeded(self) -> None:
        max_ips = 3
        limiter = self._make_limiter(limit=5, window=10.0, max_ips=max_ips)

        # Register IPs 1–3 to fill the tracker dict
        limiter.check("1.1.1.1")
        limiter.check("2.2.2.2")
        limiter.check("3.3.3.3")
        assert len(limiter._trackers) == max_ips

        # Adding a 4th IP triggers half-eviction (3//2 = 1 entry evicted)
        limiter.check("4.4.4.4")
        # After half-eviction: oldest half (ceil or floor of 4/2) removed
        assert len(limiter._trackers) <= max_ips
        # The newest IP should still be present
        assert "4.4.4.4" in limiter._trackers

    def test_evicted_ip_gets_new_tracker_on_next_request(self) -> None:
        max_ips = 2
        limiter = self._make_limiter(limit=2, window=5.0, max_ips=max_ips)

        # Fill up and exhaust IP 1
        limiter.check("1.1.1.1")
        limiter.check("1.1.1.1")

        # IP 2 pushes out IP 1 via eviction
        limiter.check("2.2.2.2")
        limiter.check("2.2.2.2")

        # IP 3 triggers eviction of oldest entries
        limiter.check("3.3.3.3")

        # IP 1 was evicted, so a new tracker is created — fresh state
        allowed, _, _ = limiter.check("1.1.1.1")
        assert allowed, "Evicted IP should get a fresh tracker"


class TestPathValidation:
    """Verify that only known paths are served; everything else gets 404."""

    @pytest.mark.parametrize("allowed_path", ["/", "/version", "/metrics"])
    async def test_allowed_paths_pass_through(self, allowed_path: str) -> None:
        """Known paths should not be rejected by the path validation middleware."""
        client = await _create_test_client(_make_server())
        try:
            resp = await client.get(allowed_path)
            assert resp.status != 404, (
                f"Allowed path {allowed_path!r} should not be rejected as 404"
            )
        finally:
            await client.close()

    @pytest.mark.parametrize(
        "unknown_path",
        [
            "/unknown",
            "/admin",
            "/health/../etc/passwd",
            "/favicon.ico",
            "/robots.txt",
            "/.env",
            "/api/v1/data",
            "/healthcheck",
            "/status",
        ],
    )
    async def test_unknown_paths_return_404(self, unknown_path: str) -> None:
        client = await _create_test_client(_make_server())
        try:
            resp = await client.get(unknown_path)
            assert resp.status == 404, (
                f"Unknown path {unknown_path!r} should return 404, got {resp.status}"
            )
            text = await resp.text()
            assert "Not Found" in text
        finally:
            await client.close()

    async def test_path_validation_rejects_before_rate_limit_counting(self) -> None:
        """Repeated requests to unknown paths should not count against rate limits."""
        client = await _create_test_client(_make_server())
        try:
            # Hammer an unknown path
            for _ in range(20):
                resp = await client.get("/definitely-not-a-route")
                assert resp.status == 404

            # A legitimate request should still be allowed (not rate-limited)
            resp = await client.get("/version")
            assert resp.status == 200
        finally:
            await client.close()
