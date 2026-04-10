"""
src/health/server.py — HTTP health check endpoint for monitoring.

Provides a lightweight HTTP server with /health endpoint using aiohttp.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from src.health.models import HealthStatus, ComponentHealth, HealthReport
from src.health.checks import (
    check_database,
    check_neonize,
    check_llm_credentials,
    get_token_usage_stats,
)

if TYPE_CHECKING:
    from src.db import Database
    from src.channels.whatsapp import NeonizeBackend

log = logging.getLogger(__name__)


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
        self._runner: Optional[Any] = None
        self._site: Optional[Any] = None
        self._port: int = 8080

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
            components.append(
                await check_llm_credentials(self._llm_api_key, self._llm_base_url)
            )

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

        token_usage = None
        if self._include_token_usage:
            token_usage = get_token_usage_stats()

        return HealthReport(components=components, token_usage=token_usage)

    async def _handle_health(self, request: Any) -> Any:
        """Handle GET /health requests."""
        from aiohttp import web

        report = await self._get_health_report()
        status_code = 200 if report.status != HealthStatus.UNHEALTHY else 503
        return web.json_response(report.to_dict(), status=status_code)

    async def _handle_root(self, request: Any) -> Any:
        """Handle GET / requests with basic info."""
        from aiohttp import web

        return web.json_response(
            {
                "name": "custombot",
                "message": "Bot is running. Use /health for health check.",
            }
        )

    async def start(self, port: int = 8080, host: str = "0.0.0.0") -> None:
        """Start the health check HTTP server."""
        from aiohttp import web

        self._port = port
        app = web.Application()
        app.router.add_get("/", self._handle_root)
        app.router.add_get("/health", self._handle_health)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host, port)
        await self._site.start()
        log.info("Health check server started on http://%s:%d", host, port)

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
) -> HealthServer:
    """Create and start a health server. Convenience function for quick setup."""
    server = HealthServer(
        db=db,
        neonize_backend=neonize_backend,
        check_whatsapp=check_whatsapp,
    )
    await server.start(port=port)
    return server
