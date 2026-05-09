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

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from src.core.errors import NonCriticalCategory, log_noncritical
from src.health.checks import (
    check_database,
    check_db_changelog,
    check_db_write_breaker,
    check_disk_usage,
    check_disk_space_health,
    check_llm_circuit_breaker,
    check_llm_credentials,
    check_llm_logs,
    check_neonize,
    check_scheduler,
    check_semaphore,
    check_skill_breakers,
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
from src.core.dedup import NullDedupService
from src.health.models import ComponentHealth, HealthReport, HealthStatus
from src.health.registry import HealthCheckRegistry
from src.health.prometheus import (
    build_circuit_breaker_prometheus_output,
    build_db_changelog_prometheus_output,
    build_db_write_breaker_prometheus_output,
    build_dedup_prometheus_output,
    build_event_bus_prometheus_output,
    build_prometheus_output,
    build_scheduler_prometheus_output,
    build_semaphore_prometheus_output,
    build_skill_breakers_prometheus_output,
    build_token_cost_prometheus_output,
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
        llm: Any = None,
        dedup: Any = None,
        tool_executor: Any = None,
        app: Any = None,
    ) -> None:
        self._check_whatsapp = check_whatsapp
        self._check_llm = check_llm
        self._check_memory = check_memory
        self._check_performance = check_performance
        self._include_token_usage = include_token_usage
        self._token_usage = token_usage
        self._bot = bot
        self._db = db
        self._scheduler = scheduler
        self._llm_log_dir = llm_log_dir
        self._workspace_dir = workspace_dir
        self._shutdown_mgr = shutdown_mgr
        self._neonize_backend = neonize_backend
        self._has_db = db is not None
        self._startup_durations = startup_durations
        self._startup_total_seconds: Optional[float] = None
        self._runner: Optional[Any] = None
        self._site: Optional[Any] = None
        self._port: int = 8080
        self._llm = llm
        self._dedup = dedup if dedup is not None else NullDedupService()
        self._tool_executor = tool_executor
        self._app = app

        # Build the health check registry from constructor dependencies.
        self._registry = self._build_registry(
            db=db,
            neonize_backend=neonize_backend,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url or "https://api.openai.com/v1",
            check_whatsapp=check_whatsapp,
            check_llm=check_llm,
            check_memory=check_memory,
            check_performance=check_performance,
            bot=bot,
            scheduler=scheduler,
            llm_log_dir=llm_log_dir,
            workspace_dir=workspace_dir,
            vector_memory=vector_memory,
            sqlite_pool=sqlite_pool,
            llm=llm,
            tool_executor=tool_executor,
            app=app,
        )

    @staticmethod
    def _build_registry(
        *,
        db: Optional["Database"],
        neonize_backend: Optional["NeonizeBackend"],
        llm_api_key: Optional[str],
        llm_base_url: str,
        check_whatsapp: bool,
        check_llm: bool,
        check_memory: bool,
        check_performance: bool,
        bot: Optional["Bot"],
        scheduler: Optional["TaskScheduler"],
        llm_log_dir: Optional[str],
        workspace_dir: Optional[str],
        vector_memory: Any,
        sqlite_pool: Any,
        llm: Any,
        tool_executor: Any,
        app: Any,
    ) -> HealthCheckRegistry:
        """Register all health checks with their bound dependencies."""
        registry = HealthCheckRegistry()

        # Database
        if db is not None:
            registry.register(check_database, db=db)
        else:
            registry.register(
                lambda: ComponentHealth(
                    name="database",
                    status=HealthStatus.UNHEALTHY,
                    message="Database not configured",
                ),
                name="database",
            )

        # WhatsApp / neonize
        if check_whatsapp:
            registry.register(check_neonize, backend=neonize_backend)

        # LLM credentials
        if check_llm and llm_api_key:
            registry.register(
                check_llm_credentials, api_key=llm_api_key, base_url=llm_base_url
            )

        # System memory (psutil-based)
        if check_memory:
            registry.register(
                HealthServer._make_monitoring_check(
                    "memory",
                    "src.monitoring",
                    "check_memory_health",
                    fallback_message="psutil not installed",
                )
            )

        # Performance metrics
        if check_performance:
            registry.register(
                HealthServer._make_monitoring_check(
                    "performance",
                    "src.monitoring",
                    "check_performance_health",
                    fallback_message="Performance metrics not available",
                )
            )

        # Bot wiring
        if bot is not None:
            registry.register(
                HealthServer._make_wiring_check(bot),
                name="wiring",
            )

        # Scheduler
        registry.register(check_scheduler, scheduler=scheduler)

        # VectorMemory
        registry.register(check_vector_memory, vector_memory=vector_memory)

        # SQLite pool
        registry.register(check_sqlite_pool, pool=sqlite_pool)

        # LLM logs
        registry.register(check_llm_logs, log_dir=llm_log_dir)

        # Disk usage (requires workspace_dir)
        if workspace_dir:
            registry.register(check_disk_usage, workspace_dir=workspace_dir)
            registry.register(check_disk_space_health, workspace_dir=workspace_dir)
            registry.register(
                HealthServer._make_workspace_monitor_check(workspace_dir),
                name="workspace",
            )

        # Circuit breaker aggregation
        registry.register(check_db_write_breaker, db=db)
        registry.register(check_db_changelog, db=db)
        registry.register(check_llm_circuit_breaker, llm=llm)

        # Per-skill circuit breakers
        registry.register(check_skill_breakers, tool_executor=tool_executor)

        # Message semaphore utilization
        registry.register(check_semaphore, app=app)

        return registry

    @staticmethod
    def _make_monitoring_check(
        component_name: str,
        module_path: str,
        function_name: str,
        *,
        fallback_message: str,
    ) -> Any:
        """Create a check function that lazily imports a monitoring module."""

        async def _check() -> ComponentHealth:
            try:
                mod = __import__(module_path, fromlist=[function_name])
                fn = getattr(mod, function_name)
                result = await fn()
                if isinstance(result, dict) and "component" in result:
                    return result["component"]
                return ComponentHealth(
                    name=component_name,
                    status=HealthStatus.DEGRADED,
                    message=f"Unexpected result from {function_name}",
                )
            except ImportError:
                return ComponentHealth(
                    name=component_name,
                    status=HealthStatus.DEGRADED,
                    message=fallback_message,
                )
            except Exception as exc:
                log.debug("%s health check error: %s", component_name, exc)
                return ComponentHealth(
                    name=component_name,
                    status=HealthStatus.DEGRADED,
                    message=f"{component_name} check error: {type(exc).__name__}",
                )

        _check.__name__ = f"check_{component_name}"
        return _check

    @staticmethod
    def _make_wiring_check(bot: "Bot") -> Any:
        """Create a wiring check that validates bot component wiring."""

        def _check() -> ComponentHealth:
            wiring_result = bot.validate_wiring()
            return check_wiring(wiring_result)

        _check.__name__ = "check_wiring"
        return _check

    @staticmethod
    def _make_workspace_monitor_check(workspace_dir: str) -> Any:
        """Create a workspace health check that lazily imports the monitor."""

        async def _check() -> ComponentHealth:
            try:
                from src.monitoring.workspace_monitor import check_workspace_health

                ws_result = await check_workspace_health(workspace_dir)
                if isinstance(ws_result, dict) and "component" in ws_result:
                    return ws_result["component"]
                return ComponentHealth(
                    name="workspace",
                    status=HealthStatus.HEALTHY,
                    message="Workspace OK",
                )
            except Exception as exc:
                log.debug("Workspace health check error: %s", exc)
                return ComponentHealth(
                    name="workspace",
                    status=HealthStatus.DEGRADED,
                    message=f"Workspace check error: {type(exc).__name__}",
                )

        _check.__name__ = "check_workspace"
        return _check

    def update_startup_durations(self, durations: dict[str, float]) -> None:
        """Replace the startup-durations snapshot with the final, complete data.

        Called once after all startup steps finish so that ``/health`` returns
        timing for *every* component — not just the steps that happened to run
        before the Health Server was created.
        """
        self._startup_durations = durations
        self._startup_total_seconds = sum(durations.values())

    def _get_token_cost(self) -> dict[str, Any] | None:
        """Return token cost breakdown from TokenCostTracker if available."""
        try:
            from src.llm.cost_tracker import TokenCostTracker

            tracker = getattr(self._token_usage, "_cost_tracker", None)
            if tracker is not None and isinstance(tracker, TokenCostTracker):
                return tracker.to_dict()
        except Exception:
            pass
        return None

    async def _get_health_report(self) -> HealthReport:
        """Run all registered health checks and return a report."""
        token_usage = None
        if self._include_token_usage:
            token_usage = get_token_usage_stats(self._token_usage)

        return await self._registry.run_all(
            token_usage=token_usage,
            startup_durations=self._startup_durations,
            startup_total_seconds=self._startup_total_seconds,
            token_cost=self._get_token_cost(),
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
                self._shutdown_mgr.accepting_messages if self._shutdown_mgr is not None else False
            ),
            neonize_backend=self._neonize_backend,
            bot_wired=self._bot is not None,
            db_available=self._has_db,
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
                    "waiting-for-qr" if not self._neonize_backend.is_ready else "disconnected"
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

    async def _handle_startup(self, request: Any) -> Any:
        """Handle GET /health/startup — per-component startup timing breakdown.

        Returns a structured JSON object with the duration (seconds) of each
        startup phase and the overall total.  Responds 503 when startup data
        has not yet been collected (i.e. the bot is still booting).
        """
        from aiohttp import web

        if self._startup_durations is None:
            return web.json_response(
                {"status": "pending", "message": "Startup not yet complete"},
                status=503,
            )

        body: dict[str, Any] = {
            "status": "complete",
            "components": self._startup_durations,
            "total_seconds": round(self._startup_total_seconds or 0.0, 3),
        }
        return web.json_response(body)

    async def _handle_detailed(self, request: Any) -> Any:
        """Handle GET /health/detailed — dashboard-ready structured metrics.

        Returns a structured JSON with all key metric categories suitable
        for Grafana dashboard panels: system, llm, tools, database, queue,
        quality, anomaly, and degradation.
        """
        from aiohttp import web

        from src.monitoring.performance import get_metrics_collector

        try:
            metrics = get_metrics_collector()
            await metrics.refresh_system_metrics()
            snapshot = metrics.get_snapshot(include_system=True)
            data = snapshot.to_dict()

            # Restructure for dashboard consumption
            dashboard: dict[str, Any] = {
                "status": "ok",
                "system": data.get("system", {}),
                "llm": {
                    "latency": data.get("llm", {}).get("latency", {}),
                    "call_count": data.get("llm", {}).get("call_count", 0),
                    "histogram": data.get("llm", {}).get("histogram", {}),
                },
                "tools": {
                    "call_count": data.get("skills", {}).get("call_count", 0),
                    "per_skill": data.get("skills", {}).get("per_skill", {}),
                    "success_rate": 0.0,
                },
                "database": {
                    "write_latency": data.get("database", {}).get("write_latency", {}),
                    "retry_budget_ratio": data.get("database", {}).get("retry_budget_ratio", 1.0),
                },
                "queue": {
                    "depth": data.get("queue", {}).get("depth", 0),
                    "max_depth": data.get("queue", {}).get("max_depth", 0),
                },
                "quality": data.get("quality", {}),
                "anomaly": data.get("anomaly", {}),
                "degradation": {
                    "current_level": data.get("error_rates", {}),
                },
            }

            # Compute overall tool success rate
            per_skill = dashboard["tools"]["per_skill"]
            total_calls = sum(s.get("calls", 0) for s in per_skill.values())
            total_errors = sum(s.get("errors", 0) for s in per_skill.values())
            if total_calls > 0:
                dashboard["tools"]["success_rate"] = round(
                    1.0 - total_errors / total_calls, 4
                )

            return web.json_response(dashboard)
        except Exception as exc:
            log.error("Detailed health endpoint error: %s", exc, exc_info=True)
            return web.json_response(
                {"status": "error", "message": type(exc).__name__},
                status=500,
            )

    async def _handle_health_metrics(self, request: Any) -> Any:
        """Handle GET /health/metrics — Prometheus-compatible metrics.

        Alias for /metrics, provided for convenience so all health-related
        endpoints are under /health/*.
        """
        return await self._handle_metrics(request)

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

            # Token cost estimation (if token_usage has estimate_cost_usd)
            estimated_cost = None
            if self._token_usage and hasattr(self._token_usage, "estimate_cost_usd"):
                estimated_cost = self._token_usage.estimate_cost_usd()

            output = build_prometheus_output(
                token_usage,
                snapshot,
                llm_log_bytes,
                db_size_bytes,
                workspace_size_bytes,
                workspace_growth,
                disk_free_bytes,
                disk_total_bytes,
                per_chat_tokens=per_chat,
                estimated_cost_usd=estimated_cost,
            )
            output += build_scheduler_prometheus_output(self._scheduler)
            # LLM circuit breaker metrics (accessed directly, not through Bot)
            cb = self._llm.circuit_breaker if self._llm is not None else None
            output += build_circuit_breaker_prometheus_output(cb)
            # DB write circuit breaker metrics (accessed directly, not through Bot)
            db_cb = self._db.write_breaker if self._db is not None else None
            db_budget = self._db.retry_budget_remaining if self._db is not None else None
            db_budget_resets = self._db.retry_budget_resets if self._db is not None else None
            output += build_db_write_breaker_prometheus_output(db_cb, db_budget, db_budget_resets)
            # DB changelog stats
            db_changelog = self._db.changelog_stats if self._db is not None else None
            output += build_db_changelog_prometheus_output(db_changelog)
            # Dedup service metrics (accessed directly, not through Bot)
            dedup_stats = self._dedup.stats
            output += build_dedup_prometheus_output(dedup_stats)
            # Per-skill circuit breaker metrics
            output += build_skill_breakers_prometheus_output(self._tool_executor)
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
            # Message semaphore utilization metrics
            sem_stats = self._app.semaphore_stats if self._app is not None else None
            output += build_semaphore_prometheus_output(sem_stats)
            # Per-model token cost estimation metrics
            output += build_token_cost_prometheus_output(self._get_token_cost())
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

        # Request size limits (reject bodies and oversized URLs before path validation)
        max_body, max_url = load_request_size_config()
        middlewares.append(create_request_size_limit_middleware(max_body, max_url))

        # Path validation (reject unknown paths before rate-limit counting)
        from src.constants import HEALTH_ALLOWED_PATHS

        middlewares.append(create_path_validation_middleware(HEALTH_ALLOWED_PATHS))

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
        app.router.add_get("/health/startup", self._handle_startup)
        app.router.add_get("/health/detailed", self._handle_detailed)
        app.router.add_get("/health/metrics", self._handle_health_metrics)
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
