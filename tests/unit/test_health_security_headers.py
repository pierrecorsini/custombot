"""
Tests for src/health/server.py — security headers on all responses.

Verifies that every HTTP response from the health server includes the
required defense-in-depth headers:
- X-Content-Type-Options: nosniff
- Content-Security-Policy: default-src 'none'
- X-Frame-Options: DENY
- Cache-Control: no-store
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

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

    from src.health.server import (
        _create_rate_limit_middleware,
        _create_request_size_limit_middleware,
        _load_rate_limit_config,
        _load_request_size_config,
    )

    max_body, max_url = _load_request_size_config()
    limit, window, max_ips = _load_rate_limit_config()
    ip_limiter = _IPLimiter_shim(limit, window, max_ips)

    middlewares = [
        _create_request_size_limit_middleware(max_body, max_url),
        _create_rate_limit_middleware(ip_limiter),
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


def _IPLimiter_shim(limit: int, window: float, max_ips: int) -> Any:
    """Create a real _IPLimiter — import helper to avoid private-name mangling."""
    from src.health.server import _IPLimiter

    return _IPLimiter(limit, window, max_ips)


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
