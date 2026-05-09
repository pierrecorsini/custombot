"""
disk.py — Disk space validation utilities.

Provides cross-platform disk space checking to prevent write failures
and data corruption from disk full conditions.

Usage:
    from src.utils.disk import check_disk_space, DiskSpaceResult

    result = check_disk_space("/path/to/dir", min_bytes=100_000_000)
    if not result.has_sufficient_space:
        raise DatabaseError("Insufficient disk space")
"""

from __future__ import annotations

import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from src.utils.locking import ThreadLock

log = logging.getLogger(__name__)

PathLike = Union[str, Path]

# Default minimum free space: 100MB
DEFAULT_MIN_DISK_SPACE: int = 100 * 1024 * 1024  # 100 MB in bytes

# Warning threshold: 1GB - log warning when below this
DISK_SPACE_WARNING_THRESHOLD: int = 1024 * 1024 * 1024  # 1 GB in bytes

# Cache TTL for disk space checks (seconds)
DISK_SPACE_CACHE_TTL: int = 30

# Thread-safe cache: resolved_path → (timestamp, total, used, free)
_disk_cache: dict[str, tuple[float, int, int, int]] = {}
_disk_cache_lock = ThreadLock()


@dataclass(slots=True)
class DiskSpaceResult:
    """
    Result of a disk space check operation.

    Attributes:
        has_sufficient_space: True if available space meets minimum requirement
        total_bytes: Total disk capacity in bytes
        used_bytes: Used disk space in bytes
        free_bytes: Available disk space in bytes
        min_required_bytes: Minimum required free space in bytes
        path_checked: The path that was checked
    """

    has_sufficient_space: bool
    total_bytes: int
    used_bytes: int
    free_bytes: int
    min_required_bytes: int
    path_checked: str

    @property
    def free_mb(self) -> float:
        """Return free space in megabytes."""
        return self.free_bytes / (1024 * 1024)

    @property
    def free_gb(self) -> float:
        """Return free space in gigabytes."""
        return self.free_bytes / (1024 * 1024 * 1024)

    @property
    def usage_percent(self) -> float:
        """Return disk usage as a percentage (0-100)."""
        if self.total_bytes == 0:
            return 0.0
        return (self.used_bytes / self.total_bytes) * 100

    def to_dict(self) -> dict:
        """Convert to dictionary for logging/serialization."""
        return {
            "has_sufficient_space": self.has_sufficient_space,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "free_bytes": self.free_bytes,
            "free_mb": round(self.free_mb, 2),
            "free_gb": round(self.free_gb, 2),
            "usage_percent": round(self.usage_percent, 1),
            "min_required_bytes": self.min_required_bytes,
            "path_checked": self.path_checked,
        }


def check_disk_space(
    path: PathLike,
    min_bytes: int = DEFAULT_MIN_DISK_SPACE,
) -> DiskSpaceResult:
    """
    Check if there is sufficient disk space at the given path.

    Works cross-platform on Windows, Linux, and macOS using shutil.disk_usage().

    Args:
        path: Directory path to check. If the path doesn't exist,
              checks the nearest existing parent directory.
        min_bytes: Minimum required free space in bytes (default: 100MB)

    Returns:
        DiskSpaceResult with space information and sufficiency check.

    Raises:
        OSError: If unable to determine disk space (e.g., invalid path,
                 network drive unavailable, permission denied)

    Example:
        >>> result = check_disk_space("/data", min_bytes=1_000_000_000)
        >>> if not result.has_sufficient_space:
        ...     print(f"Only {result.free_gb:.2f}GB free, need 1GB")
        >>> if result.free_bytes < DISK_SPACE_WARNING_THRESHOLD:
        ...     log.warning("Disk space low: %.2fGB free", result.free_gb)
    """
    path_obj = Path(path)

    # Find an existing path to check (handles non-existent directories)
    check_path = path_obj
    while not check_path.exists() and check_path != check_path.parent:
        check_path = check_path.parent

    # If we reached root and it doesn't exist, use original path
    if not check_path.exists():
        check_path = path_obj

    cache_key = str(check_path.resolve())
    now = time.monotonic()

    # Fast path: return cached result if still fresh
    with _disk_cache_lock:
        cached = _disk_cache.get(cache_key)
    if cached is not None:
        ts, total, used, free = cached
        if now - ts < DISK_SPACE_CACHE_TTL:
            has_sufficient = free >= min_bytes
            result = DiskSpaceResult(
                has_sufficient_space=has_sufficient,
                total_bytes=total,
                used_bytes=used,
                free_bytes=free,
                min_required_bytes=min_bytes,
                path_checked=str(check_path),
            )
            if not has_sufficient:
                log.warning(
                    "Insufficient disk space at %s: %.2fMB free, need %.2fMB",
                    check_path,
                    result.free_mb,
                    min_bytes / (1024 * 1024),
                )
            return result

    try:
        usage = shutil.disk_usage(str(check_path))
    except OSError as exc:
        log.error("Failed to check disk space for %s: %s", check_path, exc)
        raise

    total = usage.total
    used = usage.used
    free = usage.free

    # Update cache for next caller
    with _disk_cache_lock:
        _disk_cache[cache_key] = (now, total, used, free)

    has_sufficient = free >= min_bytes

    result = DiskSpaceResult(
        has_sufficient_space=has_sufficient,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        min_required_bytes=min_bytes,
        path_checked=str(check_path),
    )

    # Log warning if disk space is low (below 1GB)
    if free < DISK_SPACE_WARNING_THRESHOLD:
        log.warning(
            "Low disk space at %s: %.2fMB free (%.1f%% used)",
            check_path,
            result.free_mb,
            result.usage_percent,
        )

    if not has_sufficient:
        log.warning(
            "Insufficient disk space at %s: %.2fMB free, need %.2fMB",
            check_path,
            result.free_mb,
            min_bytes / (1024 * 1024),
        )

    return result


def ensure_disk_space(
    path: PathLike,
    min_bytes: int = DEFAULT_MIN_DISK_SPACE,
) -> DiskSpaceResult:
    """
    Ensure sufficient disk space, raising an error if insufficient.

    Convenience function that checks disk space and raises an exception
    if the minimum requirement is not met.

    Args:
        path: Directory path to check
        min_bytes: Minimum required free space in bytes (default: 100MB)

    Returns:
        DiskSpaceResult with space information.

    Raises:
        OSError: If disk space is insufficient or check fails.

    Example:
        >>> try:
        ...     ensure_disk_space("/data", min_bytes=500_000_000)
        ... except OSError as exc:
        ...     print(f"Cannot write: {exc}")
    """
    result = check_disk_space(path, min_bytes)

    if not result.has_sufficient_space:
        raise OSError(
            f"Insufficient disk space at {result.path_checked}: "
            f"{result.free_mb:.2f}MB free, need {min_bytes / (1024 * 1024):.2f}MB"
        )

    return result


def recursive_dir_size(directory: Path) -> int:
    """Return total size (bytes) of all files under *directory*, recursively.

    Safe for any directory — skips files that raise ``OSError``.
    """
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


__all__ = [
    "check_disk_space",
    "ensure_disk_space",
    "DiskSpaceResult",
    "DEFAULT_MIN_DISK_SPACE",
    "DISK_SPACE_WARNING_THRESHOLD",
    "recursive_dir_size",
]
