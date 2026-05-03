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

import logging
from typing import TYPE_CHECKING, Any, Optional

from src.core.errors import NonCriticalCategory, log_noncritical
from src.health.checks import (
    check_database,
    check_disk_usage,
    check_disk_space_health,
    check_llm_credentials,
    check_llm_logs,
    check_neonize,
    check_scheduler,
    check_sqlite_pool,
    check_vector_memory,
    check_wiring,
    get_token_usage_stats,
)
from src.health.middleware import (
    IPLimiter,
    SecretRedactingFilter,
    create_hmac_middleware,
    create_method_validation_middleware,
    create_path_validation_middleware,
    create_rate_limit_middleware,
    create_request_size_limit_middleware,
    load_hmac_secret,
    load_rate_limit_config,
    load_request_size_config,
    mask_hmac_header,
    verify_hmac,
)
from src.health.models import ComponentHealth, HealthReport, HealthStatus
from src.health.prometheus import (
    build_circuit_breaker_prometheus_output,
    build_db_write_breaker_prometheus_output,
    build_dedup_prometheus_output,
    build_event_bus_prometheus_output,
    build_prometheus_output,
    build_scheduler_prometheus_output,
    redact_chat_id,
)

if TYPE_CHECKING:
    from src.bot import Bot
    from src.channels.neonize_backend import NeonizeBackend
    from src.db import Database
    from src.scheduler import TaskScheduler
    from src.shutdown import GracefulShutdown

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
        token_usage: Any = None,
        bot: Optional["Bot"] = None,
        scheduler: Optional["TaskScheduler"] = None,
        llm_log_dir: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        shutdown_mgr: Optional["GracefulShutdown"] = None,
        startup_durations: Optional[dict[str, float]] = None,
        vector_memory: Any = None,
        sqlite_pool: Any = None,
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
        self._vector_memory = vector_memory
        self._sqlite_pool = sqlite_pool
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
            except Exception as exc:
                log.debug("Memory health check error: %s", exc)
                components.append(
                    ComponentHealth(
                        name="memory",
                        status=HealthStatus.DEGRADED,
                        message=f"Memory check error: {type(exc).__name__}",
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
            except Exception as exc:
                log.debug("Performance health check error: %s", exc)
                components.append(
                    ComponentHealth(
                        name="performance",
                        status=HealthStatus.DEGRADED,
                        message=f"Performance check error: {type(exc).__name__}",
                    )
                )

        # Wiring validation (startup component wiring)
        if self._bot is not None:
            try:
                wiring_result = self._bot.validate_wiring()
                components.append(check_wiring(wiring_result))
            except Exception as exc:
                log.debug("Wiring health check error: %s", exc)
                components.append(
                    ComponentHealth(
                        name="wiring",
                        status=HealthStatus.UNHEALTHY,
                        message=f"Wiring check failed: {type(exc).__name__}",
                    )
                )

        # Scheduler status
        try:
            components.append(check_scheduler(self._scheduler))
        except Exception as exc:
            log.debug("Scheduler health check error: %s", exc)
            components.append(
                ComponentHealth(
                    name="scheduler",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Scheduler check failed: {type(exc).__name__}",
                )
            )

        # VectorMemory degradation status
        try:
            components.append(check_vector_memory(self._vector_memory))
        except Exception as exc:
            log.debug("VectorMemory health check error: %s", exc)
            components.append(
                ComponentHealth(
                    name="vector_memory",
                    status=HealthStatus.DEGRADED,
                    message=f"VectorMemory check failed: {type(exc).__name__}",
                )
            )

        # SQLite connection pool status
        try:
            components.append(check_sqlite_pool(self._sqlite_pool))
        except Exception as exc:
            log.debug("SQLite pool health check error: %s", exc)
            components.append(
                ComponentHealth(
                    name="sqlite_pool",
                    status=HealthStatus.DEGRADED,
                    message=f"SQLite pool check failed: {type(exc).__name__}",
                )
            )

        # LLM log directory status
        try:
            components.append(check_llm_logs(self._llm_log_dir))
        except Exception as exc:
            log.debug("LLM logs health check error: %s", exc)
            components.append(
                ComponentHealth(
                    name="llm_logs",
                    status=HealthStatus.DEGRADED,
                    message=f"LLM logs check failed: {type(exc).__name__}",
                )
            )

        # Disk usage for database and workspace directories
        if self._workspace_dir:
            try:
                components.append(check_disk_usage(self._workspace_dir))
            except Exception as exc:
                log.debug("Disk usage health check error: %s", exc)
                components.append(
                    ComponentHealth(
                        name="disk_usage",
                        status=HealthStatus.DEGRADED,
                        message=f"Disk usage check failed: {type(exc).__name__}",
                    )
                )

            # Filesystem-level free disk space check
            try:
                components.append(check_disk_space_health(self._workspace_dir))
            except Exception as exc:
                log.debug("Disk space health check error: %s", exc)
                components.append(
                    ComponentHealth(
                        name="disk_space",
                        status=HealthStatus.DEGRADED,
                        message=f"Disk space check failed: {type(exc).__name__}",
                    )
                )

            # Workspace monitor cleanup stats
            try:
                from src.monitoring.workspace_monitor import check_workspace_health

                ws_result = await check_workspace_health(self._workspace_dir)
                if "component" in ws_result:
                    components.append(ws_result["component"])
            except Exception as exc:
                log.debug("Workspace health check error: %s", exc)

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

                # Run blocking I/O in thread pool to avoid stalling the event loop
                def _compute_sizes() -> tuple[int, int]:
                    db_sz = _recursive_dir_size(data_dir) if data_dir.exists() else 0
                    ws_sz = _recursive_dir_size(ws)
                    return db_sz, ws_sz

                db_size_bytes, workspace_size_bytes = await asyncio.to_thread(_compute_sizes)

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
                    log_noncritical(
                        NonCriticalCategory.METRICS,
                        "Failed to collect workspace growth metric",
                        logger=log,
                    )
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

            output = build_prometheus_output(
                token_usage, snapshot, llm_log_bytes, db_size_bytes,
                workspace_size_bytes, workspace_growth,
                disk_free_bytes, disk_total_bytes,
                per_chat_tokens=per_chat,
            )
            output += build_scheduler_prometheus_output(self._scheduler)
            # Circuit breaker metrics (via public Bot accessor)
            cb = self._bot.get_llm_status() if self._bot is not None else None
            output += build_circuit_breaker_prometheus_output(cb)
            # DB write circuit breaker metrics
            db_cb = self._bot.get_db_write_breaker() if self._bot is not None else None
            output += build_db_write_breaker_prometheus_output(db_cb)
            # Dedup service metrics (via public Bot accessor)
            dedup_stats = self._bot.get_dedup_stats() if self._bot is not None else None
            output += build_dedup_prometheus_output(dedup_stats)
            # EventBus emission and handler metrics
            try:
                from src.core.event_bus import get_event_bus
                output += build_event_bus_prometheus_output(get_event_bus())
            except Exception:
                log_noncritical(
                    NonCriticalCategory.METRICS,
                    "Failed to include event bus metrics in Prometheus output",
                    logger=log,
                )
            return web.Response(
                text=output,
                content_type="text/plain",
                charset="utf-8",
            )
        except Exception as exc:
            log.error("Metrics endpoint error: %s", exc, exc_info=True)
            return web.Response(
                text=f"# Error generating metrics: {type(exc).__name__}\n",
                status=500,
                content_type="text/plain",
                charset="utf-8",
            )

    @staticmethod
    async def _add_security_headers(request: Any, response: Any) -> None:
        """Inject security headers into every response.

        Defense-in-depth headers prevent content-type sniffing, clickjacking,
        framing, and caching of sensitive metrics data even on internal endpoints.

        ``Strict-Transport-Security`` is added only when the request arrives
        over HTTPS (directly or via a TLS-terminating proxy that sets the
        ``X-Forwarded-Proto: https`` header).  Per RFC 6797 the header MUST
        NOT be sent over plain HTTP.
        """
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'none'"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"

        # HSTS — only when served over TLS (direct or proxy-terminated)
        scheme = request.scheme
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "")
        if scheme == "https" or forwarded_proto.lower() == "https":
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains; preload"
            )

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
        middlewares.append(create_method_validation_middleware())

        # Path validation (reject unknown paths before rate-limit counting)
        from src.constants import HEALTH_ALLOWED_PATHS

        middlewares.append(create_path_validation_middleware(HEALTH_ALLOWED_PATHS))

        # Request size limits
        max_body, max_url = load_request_size_config()
        middlewares.append(
            create_request_size_limit_middleware(max_body, max_url)
        )

        # Per-IP rate limiting middleware
        limit, window, max_ips = load_rate_limit_config()
        ip_limiter = IPLimiter(limit, window, max_ips)
        middlewares.append(create_rate_limit_middleware(ip_limiter))

        # Optional HMAC authentication middleware
        hmac_secret = load_hmac_secret()
        if hmac_secret:
            middlewares.append(create_hmac_middleware(hmac_secret))

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
            _redacting = SecretRedactingFilter(secret=hmac_secret)
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
    host: str = "127.0.0.1",
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
    await server.start(port=port, host=host)
    return server
