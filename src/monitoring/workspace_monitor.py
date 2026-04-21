"""
src/monitoring/workspace_monitor.py — Workspace size monitoring and periodic cleanup.

Monitors the workspace directory for unbounded growth and performs periodic
cleanup tasks:

- Reports workspace disk usage in the ``/health`` endpoint
- Archives old JSONL conversation files into compressed ``.tar.gz``
- Prunes backup files older than a configurable threshold
- Cleans stale ``.tmp`` files left by crashed atomic writes
- Prunes LLM log files beyond the configured rotation limit

Usage:
    from src.monitoring.workspace_monitor import WorkspaceMonitor

    monitor = WorkspaceMonitor(workspace_dir="workspace")
    monitor.start_periodic_cleanup()

    # Later...
    await monitor.stop()
"""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import shutil
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.constants import (
    LLM_LOG_MAX_AGE_DAYS,
    LLM_LOG_MAX_FILES,
    WORKSPACE_ARCHIVE_MAX_AGE_DAYS,
    WORKSPACE_BACKUP_MAX_AGE_DAYS,
    WORKSPACE_CLEANUP_INTERVAL_SECONDS,
    WORKSPACE_SIZE_WARNING_MB,
    WORKSPACE_STALE_TEMP_MAX_AGE_HOURS,
)
from src.utils.singleton import get_or_create_singleton, reset_singleton

log = logging.getLogger(__name__)


@dataclass
class WorkspaceStats:
    """Snapshot of workspace disk usage and cleanup results."""

    workspace_bytes: int = 0
    data_bytes: int = 0
    logs_bytes: int = 0
    archives_bytes: int = 0

    # Cleanup counters from last sweep
    files_archived: int = 0
    backups_pruned: int = 0
    temps_cleaned: int = 0
    logs_pruned: int = 0
    errors: int = 0

    timestamp: float = field(default_factory=time.time)

    @property
    def workspace_mb(self) -> float:
        return self.workspace_bytes / (1024 * 1024)

    @property
    def data_mb(self) -> float:
        return self.data_bytes / (1024 * 1024)

    @property
    def logs_mb(self) -> float:
        return self.logs_bytes / (1024 * 1024)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "workspace_mb": round(self.workspace_mb, 1),
            "data_mb": round(self.data_mb, 1),
            "logs_mb": round(self.logs_mb, 1),
            "archives_mb": round(self.archives_bytes / (1024 * 1024), 1),
            "last_cleanup": {
                "files_archived": self.files_archived,
                "backups_pruned": self.backups_pruned,
                "temps_cleaned": self.temps_cleaned,
                "logs_pruned": self.logs_pruned,
                "errors": self.errors,
            },
            "timestamp": self.timestamp,
        }


def _recursive_dir_size(directory: Path) -> int:
    """Return total size (bytes) of all files under *directory*, recursively."""
    total = 0
    try:
        for entry in directory.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _archive_old_conversations(
    data_dir: Path,
    archives_dir: Path,
    max_age_days: int,
) -> int:
    """Archive JSONL conversation files older than *max_age_days*.

    Moves matching files into a ``.tar.gz`` archive in ``archives_dir/``.
    Returns the number of files archived.
    """
    if not data_dir.exists():
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    archives_dir.mkdir(parents=True, exist_ok=True)

    files_to_archive: list[Path] = []
    try:
        for entry in data_dir.iterdir():
            if (
                entry.is_file()
                and entry.suffix == ".jsonl"
                and entry.stat().st_mtime < cutoff
            ):
                files_to_archive.append(entry)
    except OSError as exc:
        log.warning("Failed scanning data dir for archival: %s", exc)
        return 0

    if not files_to_archive:
        return 0

    # Create a single tar.gz archive for this batch
    ts = time.strftime("%Y%m%d-%H%M%S")
    archive_path = archives_dir / f"conversations-{ts}.tar.gz"
    count = 0

    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            for f in files_to_archive:
                try:
                    tar.add(f, arcname=f.name)
                    f.unlink()
                    count += 1
                except OSError as exc:
                    log.warning("Failed to archive %s: %s", f.name, exc)

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        log.info(
            "Archived %d old conversation file(s) into %s (%.1f MB)",
            count,
            archive_path.name,
            size_mb,
        )
    except OSError as exc:
        log.warning("Failed to create archive %s: %s", archive_path, exc)

    return count


def _prune_old_backups(
    workspace: Path,
    max_age_days: int,
) -> int:
    """Remove backup files older than *max_age_days*.

    Scans for ``.bak``, ``.backup``, and ``*.bak.*`` patterns in the
    workspace root and ``.data/`` directory.
    """
    cutoff = time.time() - (max_age_days * 86400)
    count = 0

    backup_patterns = ("*.bak", "*.backup")
    search_dirs = [workspace]
    data_dir = workspace / ".data"
    if data_dir.exists():
        search_dirs.append(data_dir)

    for search_dir in search_dirs:
        for pattern in backup_patterns:
            try:
                for entry in search_dir.glob(pattern):
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        try:
                            entry.unlink()
                            count += 1
                            log.debug("Pruned backup: %s", entry.name)
                        except OSError as exc:
                            log.warning("Failed to prune backup %s: %s", entry, exc)
            except OSError as exc:
                log.debug("Error scanning for backups in %s: %s", search_dir, exc)

    if count > 0:
        log.info("Pruned %d old backup file(s)", count)
    return count


def _clean_stale_temps(
    workspace: Path,
    max_age_hours: float,
) -> int:
    """Remove ``.tmp`` files older than *max_age_hours*.

    These are left behind by crashed atomic writes.
    """
    cutoff = time.time() - (max_age_hours * 3600)
    count = 0

    try:
        for entry in workspace.rglob("*.tmp"):
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                try:
                    entry.unlink()
                    count += 1
                    log.debug("Cleaned stale temp: %s", entry)
                except OSError as exc:
                    log.warning("Failed to clean temp %s: %s", entry, exc)
    except OSError as exc:
        log.debug("Error scanning for temp files: %s", exc)

    if count > 0:
        log.info("Cleaned %d stale temp file(s)", count)
    return count


def _prune_llm_logs(
    workspace: Path,
    max_files: int,
    max_age_days: int,
) -> int:
    """Prune LLM log files beyond the configured limits.

    Removes files older than *max_age_days* first, then enforces
    the *max_files* count limit by removing the oldest files.
    """
    log_dir = workspace / "logs" / "llm"
    if not log_dir.exists():
        return 0

    cutoff = time.time() - (max_age_days * 86400)
    pruned = 0

    try:
        files = sorted(
            (f for f in log_dir.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime,
        )
    except OSError:
        return 0

    # Age-based pruning
    for f in files:
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                pruned += 1
        except OSError:
            pass

    # Count-based pruning: remove oldest files exceeding the limit
    try:
        remaining = sorted(
            (f for f in log_dir.iterdir() if f.is_file()),
            key=lambda f: f.stat().st_mtime,
        )
        while len(remaining) > max_files:
            oldest = remaining.pop(0)
            try:
                oldest.unlink()
                pruned += 1
            except OSError:
                pass
    except OSError:
        pass

    if pruned > 0:
        log.info("Pruned %d LLM log file(s)", pruned)
    return pruned


class WorkspaceMonitor:
    """Periodic workspace size monitor and cleanup task.

    Usage:
        monitor = WorkspaceMonitor(workspace_dir="workspace")
        monitor.start_periodic_cleanup()

        # Later...
        await monitor.stop()
    """

    def __init__(
        self,
        workspace_dir: str,
        cleanup_interval: float = WORKSPACE_CLEANUP_INTERVAL_SECONDS,
        archive_max_age_days: int = WORKSPACE_ARCHIVE_MAX_AGE_DAYS,
        backup_max_age_days: int = WORKSPACE_BACKUP_MAX_AGE_DAYS,
        stale_temp_max_age_hours: float = WORKSPACE_STALE_TEMP_MAX_AGE_HOURS,
        size_warning_mb: float = WORKSPACE_SIZE_WARNING_MB,
        llm_log_max_files: int = LLM_LOG_MAX_FILES,
        llm_log_max_age_days: int = LLM_LOG_MAX_AGE_DAYS,
    ) -> None:
        self._workspace = Path(workspace_dir)
        self._cleanup_interval = cleanup_interval
        self._archive_max_age_days = archive_max_age_days
        self._backup_max_age_days = backup_max_age_days
        self._stale_temp_max_age_hours = stale_temp_max_age_hours
        self._size_warning_mb = size_warning_mb
        self._llm_log_max_files = llm_log_max_files
        self._llm_log_max_age_days = llm_log_max_age_days
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._last_stats: Optional[WorkspaceStats] = None

    def get_stats(self) -> WorkspaceStats:
        """Compute current workspace size statistics (blocking I/O)."""
        workspace_bytes = _recursive_dir_size(self._workspace)
        data_dir = self._workspace / ".data"
        data_bytes = _recursive_dir_size(data_dir) if data_dir.exists() else 0
        logs_dir = self._workspace / "logs"
        logs_bytes = _recursive_dir_size(logs_dir) if logs_dir.exists() else 0
        archives_dir = self._workspace / ".data" / "archives"
        archives_bytes = _recursive_dir_size(archives_dir) if archives_dir.exists() else 0

        stats = WorkspaceStats(
            workspace_bytes=workspace_bytes,
            data_bytes=data_bytes,
            logs_bytes=logs_bytes,
            archives_bytes=archives_bytes,
        )
        self._last_stats = stats
        return stats

    def _run_cleanup(self) -> WorkspaceStats:
        """Execute all cleanup tasks (blocking). Returns updated stats."""
        stats = self.get_stats()

        try:
            stats.files_archived = _archive_old_conversations(
                data_dir=self._workspace / ".data",
                archives_dir=self._workspace / ".data" / "archives",
                max_age_days=self._archive_max_age_days,
            )
        except Exception as exc:
            log.warning("Conversation archival failed: %s", exc)
            stats.errors += 1

        try:
            stats.backups_pruned = _prune_old_backups(
                workspace=self._workspace,
                max_age_days=self._backup_max_age_days,
            )
        except Exception as exc:
            log.warning("Backup pruning failed: %s", exc)
            stats.errors += 1

        try:
            stats.temps_cleaned = _clean_stale_temps(
                workspace=self._workspace,
                max_age_hours=self._stale_temp_max_age_hours,
            )
        except Exception as exc:
            log.warning("Temp file cleanup failed: %s", exc)
            stats.errors += 1

        try:
            stats.logs_pruned = _prune_llm_logs(
                workspace=self._workspace,
                max_files=self._llm_log_max_files,
                max_age_days=self._llm_log_max_age_days,
            )
        except Exception as exc:
            log.warning("LLM log pruning failed: %s", exc)
            stats.errors += 1

        # Refresh sizes after cleanup
        if stats.files_archived or stats.backups_pruned or stats.temps_cleaned or stats.logs_pruned:
            post_stats = self.get_stats()
            stats.workspace_bytes = post_stats.workspace_bytes
            stats.data_bytes = post_stats.data_bytes
            stats.logs_bytes = post_stats.logs_bytes
            stats.archives_bytes = post_stats.archives_bytes

        return stats

    async def _periodic_cleanup(self) -> None:
        """Background task that monitors workspace size and runs cleanup."""
        log.info(
            "Workspace monitor started (interval=%.0fs, archive_after=%dd, "
            "backup_max=%dd, stale_temp_max=%.1fh, warning=%.0fMB)",
            self._cleanup_interval,
            self._archive_max_age_days,
            self._backup_max_age_days,
            self._stale_temp_max_age_hours,
            self._size_warning_mb,
        )

        while self._running:
            try:
                stats = await asyncio.to_thread(self._run_cleanup)

                if stats.workspace_mb > self._size_warning_mb:
                    log.warning(
                        "Workspace size %.1f MB exceeds warning threshold %.0f MB",
                        stats.workspace_mb,
                        self._size_warning_mb,
                    )
                else:
                    log.debug(
                        "Workspace: %.1f MB (data=%.1f, logs=%.1f, archives=%.1f)",
                        stats.workspace_mb,
                        stats.data_mb,
                        stats.logs_mb,
                        stats.archives_bytes / (1024 * 1024),
                    )

                if stats.files_archived or stats.backups_pruned or stats.temps_cleaned or stats.logs_pruned:
                    log.info(
                        "Workspace cleanup: archived=%d, backups_pruned=%d, "
                        "temps_cleaned=%d, logs_pruned=%d, errors=%d",
                        stats.files_archived,
                        stats.backups_pruned,
                        stats.temps_cleaned,
                        stats.logs_pruned,
                        stats.errors,
                    )

            except Exception as exc:
                log.error("Workspace cleanup cycle failed: %s", exc, exc_info=True)

            await asyncio.sleep(self._cleanup_interval)

    def start_periodic_cleanup(self) -> None:
        """Start periodic workspace monitoring and cleanup."""
        if self._running:
            log.warning("Workspace monitor already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._periodic_cleanup())

    async def stop(self) -> None:
        """Stop the periodic workspace monitor."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("Workspace monitor stopped")

    @property
    def last_stats(self) -> Optional[WorkspaceStats]:
        """Most recent workspace statistics."""
        return self._last_stats

    @property
    def is_running(self) -> bool:
        """Whether the monitor is actively running."""
        return self._running


def get_global_workspace_monitor(
    workspace_dir: str = "workspace",
    **kwargs,
) -> WorkspaceMonitor:
    """Get or create the global workspace monitor instance."""
    return get_or_create_singleton(
        WorkspaceMonitor,
        workspace_dir=workspace_dir,
        **kwargs,
    )


def reset_global_workspace_monitor() -> None:
    """Reset the global workspace monitor (useful for testing)."""
    reset_singleton(WorkspaceMonitor)


async def check_workspace_health(workspace_dir: str) -> dict:
    """Check workspace health for the health endpoint.

    Returns a dict with ``ComponentHealth`` and workspace stats.
    """
    from src.health import ComponentHealth, HealthStatus

    try:
        monitor = get_global_workspace_monitor(workspace_dir=workspace_dir)
        stats = await asyncio.to_thread(monitor.get_stats)

        if stats.workspace_mb > WORKSPACE_SIZE_WARNING_MB:
            status = HealthStatus.DEGRADED
            message = (
                f"Workspace size {stats.workspace_mb:.1f} MB "
                f"exceeds threshold {WORKSPACE_SIZE_WARNING_MB:.0f} MB"
            )
        else:
            status = HealthStatus.HEALTHY
            message = f"Workspace: {stats.workspace_mb:.1f} MB"

        return {
            "component": ComponentHealth(
                name="workspace",
                status=status,
                message=message,
            ),
            "stats": stats.to_dict(),
        }
    except Exception as exc:
        log.error("Workspace health check failed: %s", exc, exc_info=True)
        return {
            "component": ComponentHealth(
                name="workspace",
                status=HealthStatus.DEGRADED,
                message=f"Workspace check error: {type(exc).__name__}",
            ),
            "stats": None,
        }
