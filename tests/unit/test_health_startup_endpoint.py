"""
Tests for src/health/server.py — /health/startup endpoint.

Verifies the startup-phase timing breakdown endpoint:
- Returns 503 with ``status: "pending"`` when startup has not completed.
- Returns 200 with per-component durations and total when data is available.
- Carries the standard security headers on every response.
"""

from __future__ import annotations

from typing import Any

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


async def _create_test_client(server: HealthServer) -> Any:
    """Build an aiohttp test client wired with the /health/startup route."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from src.constants import HEALTH_ALLOWED_PATHS

    max_body, max_url = load_request_size_config()
    limit, window, max_ips = load_rate_limit_config()
    ip_limiter = IPLimiter(limit, window, max_ips)

    middlewares = [
        create_request_size_limit_middleware(max_body, max_url),
        create_path_validation_middleware(HEALTH_ALLOWED_PATHS),
        create_rate_limit_middleware(ip_limiter),
    ]

    app = web.Application(middlewares=middlewares)
    app.on_response_prepare.append(server._add_security_headers)
    app.router.add_get("/health/startup", server._handle_startup)

    test_server = TestServer(app)
    client = TestClient(test_server)
    await client.start_server()
    return client


def _make_server(**kwargs: Any) -> HealthServer:
    """Build a minimal HealthServer (no optional deps)."""
    return HealthServer(
        check_whatsapp=False,
        check_llm=False,
        check_memory=False,
        check_performance=False,
        **kwargs,
    )


# ── Tests ────────────────────────────────────────────────────────────────


class TestHealthStartupEndpoint:
    """Tests for GET /health/startup."""

    async def test_returns_503_when_startup_not_complete(self) -> None:
        """Before update_startup_durations is called, endpoint returns 503."""
        server = _make_server(startup_durations=None)
        client = await _create_test_client(server)
        try:
            resp = await client.get("/health/startup")
            assert resp.status == 503
            body = await resp.json()
            assert body["status"] == "pending"
        finally:
            await client.close()

    async def test_returns_200_with_durations_when_available(self) -> None:
        """After update_startup_durations, endpoint returns component timing."""
        durations = {"database": 0.15, "llm_client": 0.32, "vector_memory": 0.08}
        server = _make_server(startup_durations=None)
        server.update_startup_durations(durations)

        client = await _create_test_client(server)
        try:
            resp = await client.get("/health/startup")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "complete"
            assert body["components"] == durations
            assert body["total_seconds"] == round(sum(durations.values()), 3)
        finally:
            await client.close()

    async def test_total_seconds_rounded_to_3_decimals(self) -> None:
        """total_seconds is rounded to millisecond precision."""
        durations = {"step_a": 0.1234567}
        server = _make_server(startup_durations=None)
        server.update_startup_durations(durations)

        client = await _create_test_client(server)
        try:
            resp = await client.get("/health/startup")
            body = await resp.json()
            assert body["total_seconds"] == 0.123
        finally:
            await client.close()

    async def test_security_headers_present(self) -> None:
        """Responses carry the standard security header set."""
        server = _make_server(startup_durations={"x": 1.0})
        client = await _create_test_client(server)
        try:
            resp = await client.get("/health/startup")
            assert resp.headers.get("X-Content-Type-Options") == "nosniff"
            assert resp.headers.get("Cache-Control") == "no-store"
        finally:
            await client.close()

    async def test_empty_durations_returns_200(self) -> None:
        """An empty durations dict is still valid — total is 0."""
        server = _make_server(startup_durations=None)
        server.update_startup_durations({})

        client = await _create_test_client(server)
        try:
            resp = await client.get("/health/startup")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "complete"
            assert body["components"] == {}
            assert body["total_seconds"] == 0.0
        finally:
            await client.close()

    async def test_unauthenticated_request_still_served(self) -> None:
        """The endpoint works without HMAC auth (no secret configured)."""
        durations = {"db": 0.5}
        server = _make_server(startup_durations=None)
        server.update_startup_durations(durations)

        client = await _create_test_client(server)
        try:
            resp = await client.get("/health/startup")
            assert resp.status == 200
        finally:
            await client.close()
