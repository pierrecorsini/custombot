"""
src/health/checks.py — Individual health check functions.

Each function checks one component and returns a ComponentHealth result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

from src.health.models import ComponentHealth, HealthStatus

if TYPE_CHECKING:
    from src.channels.neonize_backend import NeonizeBackend
    from src.db import Database
    from src.db.sqlite_pool import SqliteConnectionPool
    from src.scheduler import TaskScheduler

log = logging.getLogger(__name__)


async def check_database(db: "Database") -> ComponentHealth:
    """Check if the database is accessible and functional.

    Tests actual file I/O by listing chats, not just in-memory state.
    """
    start = time.perf_counter()
    try:
        if not db._initialized:
            return ComponentHealth(
                name="database",
                status=HealthStatus.UNHEALTHY,
                message="Database not initialized",
            )
        # Actual I/O test: read chats from disk via the async API
        chats = await db.list_chats()
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="database",
            status=HealthStatus.HEALTHY,
            message=f"Database is accessible ({len(chats)} chats)",
            latency_ms=latency,
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        log.warning("Database health check failed: %s", exc)
        return ComponentHealth(
            name="database",
            status=HealthStatus.UNHEALTHY,
            message=f"Database error: {exc}",
            latency_ms=latency,
        )


async def check_neonize(backend: Optional["NeonizeBackend"]) -> ComponentHealth:
    """Check neonize WhatsApp connection with an active probe.

    Instead of just checking ``is_connected`` (which returns True for zombie
    connections where the WebSocket died silently), this sends a real chat
    presence update through the pipe. If the send fails or times out, the
    connection is reported as unhealthy even if ``is_connected`` is True.

    Also exposes diagnostic info: uptime, messages received, last probe result.
    """
    start = time.perf_counter()
    try:
        if backend is None:
            return ComponentHealth(
                name="whatsapp",
                status=HealthStatus.UNHEALTHY,
                message="WhatsApp backend not configured",
            )

        if not backend.is_connected:
            latency = (time.perf_counter() - start) * 1000
            return ComponentHealth(
                name="whatsapp",
                status=HealthStatus.UNHEALTHY,
                message="WhatsApp not connected",
                latency_ms=latency,
            )

        # Active probe: send data through the live WebSocket
        probe = await backend.probe_connection()
        latency = (time.perf_counter() - start) * 1000
        diag = backend.connection_diagnostics()

        details: dict[str, Any] = {
            "uptime_seconds": round(diag["uptime_seconds"], 1),
            "messages_received": diag["messages_received"],
            "probe_alive": probe.alive,
        }
        if probe.reason:
            details["probe_reason"] = probe.reason

        if probe.alive:
            return ComponentHealth(
                name="whatsapp",
                status=HealthStatus.HEALTHY,
                message="WhatsApp connected (probe OK)",
                latency_ms=latency,
                details=details,
            )

        # is_connected is True but probe failed → zombie connection
        return ComponentHealth(
            name="whatsapp",
            status=HealthStatus.UNHEALTHY,
            message=f"Zombie connection detected: {probe.reason}",
            latency_ms=latency,
            details=details,
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="whatsapp",
            status=HealthStatus.UNHEALTHY,
            message=f"WhatsApp check failed: {type(exc).__name__}",
            latency_ms=latency,
        )


async def check_llm_credentials(
    api_key: str, base_url: str, timeout: float = 5.0
) -> ComponentHealth:
    """Check if LLM credentials are valid by making a minimal API call."""
    if not api_key or api_key.startswith("sk-your"):
        return ComponentHealth(
            name="llm",
            status=HealthStatus.UNHEALTHY,
            message="API key not configured",
        )

    start = time.perf_counter()
    client = None
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        await asyncio.wait_for(client.models.list(), timeout=timeout)
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="llm",
            status=HealthStatus.HEALTHY,
            message="LLM credentials are valid",
            latency_ms=latency,
        )
    except asyncio.TimeoutError:
        latency = (time.perf_counter() - start) * 1000
        return ComponentHealth(
            name="llm",
            status=HealthStatus.DEGRADED,
            message="LLM API timeout (credentials may be valid)",
            latency_ms=latency,
        )
    except Exception as exc:
        latency = (time.perf_counter() - start) * 1000
        error_msg = str(exc).lower()
        if "401" in error_msg or "unauthorized" in error_msg or "invalid" in error_msg:
            return ComponentHealth(
                name="llm",
                status=HealthStatus.UNHEALTHY,
                message="Invalid API credentials",
                latency_ms=latency,
            )
        log.debug("LLM health check error: %s", exc)
        return ComponentHealth(
            name="llm",
            status=HealthStatus.DEGRADED,
            message=f"LLM check failed: {type(exc).__name__}",
            latency_ms=latency,
        )
    finally:
        if client is not None:
            await client.close()


def check_wiring(wiring_result: list[tuple[str, bool, str]]) -> ComponentHealth:
    """Check bot component wiring from validate_wiring() results."""
    failed = [(name, msg) for name, ok, msg in wiring_result if not ok]
    if not failed:
        return ComponentHealth(
            name="wiring",
            status=HealthStatus.HEALTHY,
            message=f"All {len(wiring_result)} components wired correctly",
        )
    names = ", ".join(name for name, _ in failed)
    return ComponentHealth(
        name="wiring",
        status=HealthStatus.UNHEALTHY,
        message=f"Missing components: {names}",
    )


def get_token_usage_stats(token_usage: Any = None) -> dict[str, Any]:
    """Get LLM token usage statistics for the current session."""
    if token_usage is not None:
        return {
            "prompt_tokens": token_usage.prompt_tokens,
            "completion_tokens": token_usage.completion_tokens,
            "total_tokens": token_usage.total_tokens,
            "request_count": token_usage.request_count,
        }
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "request_count": 0,
        "error": "Token tracking not available",
    }


def check_llm_logs(log_dir: Optional[str] = None) -> ComponentHealth:
    """Check LLM log directory size and file count.

    Reports HEALTHY when logging is disabled or directory size is reasonable,
    DEGRADED when the directory is growing large (>100 MB).
    """
    if log_dir is None:
        return ComponentHealth(
            name="llm_logs",
            status=HealthStatus.HEALTHY,
            message="LLM logging disabled",
        )

    from pathlib import Path

    from src.logging.llm_logging import _dir_size, _list_log_files

    dir_path = Path(log_dir)
    if not dir_path.exists():
        return ComponentHealth(
            name="llm_logs",
            status=HealthStatus.HEALTHY,
            message="LLM log directory not yet created",
        )

    try:
        total_bytes = _dir_size(dir_path)
        file_count = len(_list_log_files(dir_path))
        size_mb = total_bytes / (1024 * 1024)
        message = f"LLM logs: {file_count} files, {size_mb:.1f} MB"

        if size_mb > 100:
            return ComponentHealth(
                name="llm_logs",
                status=HealthStatus.DEGRADED,
                message=message,
            )
        return ComponentHealth(
            name="llm_logs",
            status=HealthStatus.HEALTHY,
            message=message,
        )
    except Exception as exc:
        return ComponentHealth(
            name="llm_logs",
            status=HealthStatus.DEGRADED,
            message=f"LLM log check error: {type(exc).__name__}",
        )


def _recursive_dir_size(directory: Path) -> int:
    """Return total size (bytes) of all files under *directory*, recursively.

    Delegates to :func:`src.utils.disk.recursive_dir_size` for reuse.
    """
    from src.utils.disk import recursive_dir_size

    return recursive_dir_size(directory)


def check_disk_usage(workspace_dir: str) -> ComponentHealth:
    """Check database and workspace disk usage.

    Reports db_size_mb (workspace/.data/) and workspace_size_mb (full workspace).
    Returns DEGRADED when workspace exceeds 1 GB.
    """
    from pathlib import Path

    workspace = Path(workspace_dir)
    if not workspace.exists():
        return ComponentHealth(
            name="disk_usage",
            status=HealthStatus.HEALTHY,
            message="Workspace directory not yet created",
        )

    data_dir = workspace / ".data"
    db_bytes = _recursive_dir_size(data_dir) if data_dir.exists() else 0
    workspace_bytes = _recursive_dir_size(workspace)

    db_mb = db_bytes / (1024 * 1024)
    workspace_mb = workspace_bytes / (1024 * 1024)

    message = f"db: {db_mb:.1f} MB, workspace: {workspace_mb:.1f} MB"
    status = HealthStatus.DEGRADED if workspace_mb > 1024 else HealthStatus.HEALTHY

    return ComponentHealth(
        name="disk_usage",
        status=status,
        message=message,
    )


def check_disk_space_health(workspace_dir: str) -> ComponentHealth:
    """Check filesystem-level free disk space on the workspace volume.

    Uses :func:`src.utils.disk.check_disk_space` to query actual free space.
    Reports DEGRADED when available space falls below the warning threshold
    (1 GB) or the default minimum (100 MB).
    """
    from src.utils.disk import DISK_SPACE_WARNING_THRESHOLD, check_disk_space

    try:
        result = check_disk_space(workspace_dir)
    except OSError as exc:
        return ComponentHealth(
            name="disk_space",
            status=HealthStatus.DEGRADED,
            message=f"Cannot check disk space: {exc}",
        )

    free_gb = result.free_bytes / (1024 * 1024 * 1024)
    if not result.has_sufficient_space:
        return ComponentHealth(
            name="disk_space",
            status=HealthStatus.DEGRADED,
            message=f"Low disk space: {free_gb:.2f} GB free",
        )
    if result.free_bytes < DISK_SPACE_WARNING_THRESHOLD:
        return ComponentHealth(
            name="disk_space",
            status=HealthStatus.DEGRADED,
            message=f"Disk space below warning threshold: {free_gb:.2f} GB free",
        )
    return ComponentHealth(
        name="disk_space",
        status=HealthStatus.HEALTHY,
        message=f"{free_gb:.2f} GB free",
    )


def check_readiness(
    *,
    shutdown_accepting: bool,
    neonize_backend: Optional["NeonizeBackend"],
    bot_wired: bool,
    db_available: bool,
) -> tuple[bool, list[str]]:
    """Evaluate Kubernetes-style readiness: all components initialized and accepting traffic.

    Returns (ready, reasons) where *ready* is True only when every signal is
    green.  *reasons* lists the failing conditions (empty when ready).
    """
    reasons: list[str] = []

    if not shutdown_accepting:
        reasons.append("shutdown in progress")

    if neonize_backend is None:
        reasons.append("WhatsApp backend not configured")
    elif not neonize_backend.is_ready:
        reasons.append("WhatsApp channel not connected")

    if not bot_wired:
        reasons.append("bot components not wired")

    if not db_available:
        reasons.append("database not available")

    return len(reasons) == 0, reasons


def check_vector_memory(vector_memory: Any) -> ComponentHealth:
    """Check VectorMemory health: embedding API reachability and retry queue depth.

    Reports DEGRADED when the embedding API is unreachable or the retry queue
    exceeds 50% capacity.  Reports UNHEALTHY when VectorMemory is not configured.
    """
    from src.vector_memory.health import _MAX_RETRY_QUEUE_SIZE

    if vector_memory is None:
        return ComponentHealth(
            name="vector_memory",
            status=HealthStatus.UNHEALTHY,
            message="VectorMemory not configured",
        )

    snap = vector_memory.health_snapshot()
    api_healthy = snap["embedding_api_healthy"]
    queue_depth = snap["retry_queue_depth"]
    queue_capacity = snap["retry_queue_capacity"]

    details: dict[str, Any] = {
        "embedding_api_healthy": api_healthy,
        "retry_queue_depth": queue_depth,
        "retry_queue_capacity": queue_capacity,
    }

    if not api_healthy and queue_capacity > 0.5:
        return ComponentHealth(
            name="vector_memory",
            status=HealthStatus.DEGRADED,
            message=(
                f"Embedding API unreachable, retry queue at {queue_depth}/{_MAX_RETRY_QUEUE_SIZE}"
            ),
            details=details,
        )

    if not api_healthy:
        return ComponentHealth(
            name="vector_memory",
            status=HealthStatus.DEGRADED,
            message="Embedding API unreachable",
            details=details,
        )

    if queue_capacity > 0.5:
        return ComponentHealth(
            name="vector_memory",
            status=HealthStatus.DEGRADED,
            message=f"Retry queue at {queue_depth}/{_MAX_RETRY_QUEUE_SIZE} (>50%)",
            details=details,
        )

    msg = "VectorMemory operational"
    if queue_depth > 0:
        msg = f"VectorMemory operational ({queue_depth} queued retries)"
    return ComponentHealth(
        name="vector_memory",
        status=HealthStatus.HEALTHY,
        message=msg,
        details=details if queue_depth > 0 else None,
    )


def check_scheduler(scheduler: Optional["TaskScheduler"]) -> ComponentHealth:
    """Check task scheduler status: running state, task count, recent failures."""
    if scheduler is None:
        return ComponentHealth(
            name="scheduler",
            status=HealthStatus.UNHEALTHY,
            message="Scheduler not configured",
        )

    status = scheduler.get_status()

    if not status["running"]:
        return ComponentHealth(
            name="scheduler",
            status=HealthStatus.UNHEALTHY,
            message="Scheduler is not running",
        )

    parts = [
        f"{status['enabled_tasks']} active tasks",
        f"{status['chats_with_tasks']} chats",
    ]
    if status["failure_count"] > 0:
        parts.append(f"{status['failure_count']} failures")

    details: dict[str, Any] = {}
    recent = status.get("recent_executions", [])
    if recent:
        details["recent_executions"] = recent

    return ComponentHealth(
        name="scheduler",
        status=HealthStatus.HEALTHY,
        message=f"Scheduler running ({', '.join(parts)})",
        details=details or None,
    )


def check_sqlite_pool(pool: Optional["SqliteConnectionPool"]) -> ComponentHealth:
    """Check the shared SQLite connection pool: active connections and their databases.

    Reports ``UNHEALTHY`` when the pool is not configured (shouldn't happen
    after startup).  Reports ``DEGRADED`` when pool utilization exceeds 80%.
    Otherwise reports ``HEALTHY`` with connection count, per-database stats,
    and pool utilization.
    """
    if pool is None:
        return ComponentHealth(
            name="sqlite_pool",
            status=HealthStatus.UNHEALTHY,
            message="SQLite connection pool not initialized",
        )

    count = pool.connection_count
    cap = pool.max_connections
    util = pool.utilization
    idle = pool.idle_count
    parts = [f"{count}/{cap} connections ({util:.0%})"]

    details: dict[str, Any] = {
        "utilization": round(util, 2),
        "idle_connections": idle,
        "per_database": pool.db_stats,
    }
    active = pool.active_connections
    if active:
        details["connections"] = active

    status = HealthStatus.HEALTHY
    if util >= 0.8:
        status = HealthStatus.DEGRADED
        parts.append("near capacity")

    return ComponentHealth(
        name="sqlite_pool",
        status=status,
        message=f"Pool active ({', '.join(parts)})",
        details=details,
    )
