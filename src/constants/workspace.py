"""Workspace constants — directory, cleanup intervals, audit log rotation, config watcher, thread pool."""

from __future__ import annotations

# ─────────────────────────────────────────────────────────────────────────────
# Workspace Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Root workspace directory for all bot data.
# Contains: .data/, auth/, logs/, skills/, whatsapp_data/
WORKSPACE_DIR: str = "workspace"

# ─────────────────────────────────────────────────────────────────────────────
# Workspace Cleanup Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default interval (seconds) between periodic workspace size checks and cleanup.
# A typical workspace has JSONL conversation files, vector memory, LLM logs,
# and backups that accumulate over time.
WORKSPACE_CLEANUP_INTERVAL_SECONDS: float = 3600.0  # 1 hour

# Maximum age (days) for JSONL conversation files before they are archived
# into a compressed .tar.gz.  Active conversations are never touched.
WORKSPACE_ARCHIVE_MAX_AGE_DAYS: int = 30

# Maximum age (days) for backup files before they are pruned.
WORKSPACE_BACKUP_MAX_AGE_DAYS: int = 7

# Maximum age (days) for stale temporary files (e.g., .tmp from crashed
# atomic writes) before they are removed.
WORKSPACE_STALE_TEMP_MAX_AGE_HOURS: float = 1.0

# Workspace size threshold (MB) at which the health check reports DEGRADED.
# Helps operators detect unbounded disk growth.
WORKSPACE_SIZE_WARNING_MB: float = 1024.0

# ─────────────────────────────────────────────────────────────────────────────
# Audit Log Rotation
# ─────────────────────────────────────────────────────────────────────────────

# Maximum age (days) for rotated audit log files (audit.{i}.jsonl).  Files
# older than this are deleted during WorkspaceMonitor's periodic cleanup cycle,
# preventing unbounded disk growth from the per-skill JSONL audit trail.
AUDIT_LOG_MAX_AGE_DAYS: int = 90

# Maximum number of rotated audit log files to retain.  When exceeded, the
# oldest files are removed during cleanup.  Matches SkillAuditLogger's
# MAX_ROTATED_FILES (5) by default, but can be raised if operators want to
# keep more historical audit trail on disk.
AUDIT_LOG_MAX_FILES: int = 5

# ─────────────────────────────────────────────────────────────────────────────
# Config Hot-Reload Watcher
# ─────────────────────────────────────────────────────────────────────────────

# How often (seconds) the config watcher polls config.json for mtime changes.
# Longer intervals reduce filesystem syscalls; shorter intervals apply changes
# faster after the file is saved.
CONFIG_WATCH_INTERVAL_SECONDS: float = 5.0

# Minimum interval (seconds) between mtime checks. Prevents redundant stat()
# calls when the watch loop fires faster than expected.
CONFIG_WATCH_DEBOUNCE_SECONDS: float = 2.0

# ─────────────────────────────────────────────────────────────────────────────
# Thread Pool Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Default maximum number of worker threads for the asyncio ThreadPoolExecutor.
# This executor backs all asyncio.to_thread() calls (database reads/writes,
# file I/O, psutil calls, vector memory operations).  Under high concurrency
# (many chats active simultaneously), the default pool
# (min(32, os.cpu_count()+4)) can saturate, causing to_thread calls to queue.
# 16 workers balances concurrency against thread overhead.
DEFAULT_THREAD_POOL_WORKERS: int = 16
