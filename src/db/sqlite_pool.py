"""
src/db/sqlite_pool.py — Bounded connection pool & factory for SQLite databases.

Provides:
- Bounded connection pool with configurable ``max_connections`` cap
- Per-database connection grouping for monitoring and WAL-mode consistency
- Connection factory with consistent WAL-mode and foreign-key configuration
- Centralized lifecycle management for graceful shutdown
- Health/status reporting per database

Usage::

    from src.db.sqlite_pool import SqliteConnectionPool
    from src.db.sqlite_utils import SqliteHelper

    pool = SqliteConnectionPool()
    SqliteHelper.set_pool(pool)

    # Components that use SqliteHelper auto-register on connect()
    # and auto-unregister on close().

    # At shutdown:
    pool.close_all()  # close any leaked connections
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.constants.db import SQLITE_POOL_MAX_CONNECTIONS
from src.core.errors import NonCriticalCategory, log_noncritical
from src.utils.locking import ThreadLock

log = logging.getLogger(__name__)

# Standard PRAGMAs applied to all pool-managed connections.
# Single source of truth — import this instead of duplicating in other modules.
DEFAULT_PRAGMAS: list[str] = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
]


@dataclass(slots=True)
class _PoolEntry:
    """Tracks a single managed SQLite connection."""

    conn: sqlite3.Connection
    path: Path


class SqliteConnectionPool:
    """Bounded connection pool and factory for all SQLite databases.

    Centralizes connection creation and lifecycle management so that:
    - All connections use consistent WAL-mode configuration
    - Total connection count is capped to limit file handle usage
    - Connections are grouped by database for monitoring and consistency
    - Graceful shutdown can close all connections in one step

    Thread-safe via ThreadLock — safe for concurrent register/unregister
    from SqliteHelper subclasses running in different threads.
    """

    def __init__(self, *, max_connections: int = SQLITE_POOL_MAX_CONNECTIONS) -> None:
        self._entries: dict[str, _PoolEntry] = {}
        self._by_path: dict[Path, list[str]] = {}
        self._max_connections = max_connections
        self._lock = ThreadLock()

    # ── connection factory ──────────────────────────────────────────────

    def get_connection(
        self,
        name: str,
        db_path: str | Path,
        *,
        pragmas: Optional[list[str]] = None,
        load_extension: bool = False,
    ) -> sqlite3.Connection:
        """Create a new SQLite connection with standard pool configuration.

        The connection is created with WAL mode and foreign keys enabled
        (or custom *pragmas*), then registered in the pool for lifecycle
        management.  Raises ``sqlite3.OperationalError`` when the pool is
        at capacity.

        Args:
            name: Unique identifier for this connection in the pool.
            db_path: Path to the database file.
            pragmas: Custom PRAGMAs (replaces defaults).
            load_extension: Enable extension loading before PRAGMAs.

        Returns:
            Configured ``sqlite3.Connection``.
        """
        with self._lock:
            if len(self._entries) >= self._max_connections:
                raise sqlite3.OperationalError(
                    f"SQLite pool at capacity ({self._max_connections} connections)"
                )

        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(path), check_same_thread=False)

        if load_extension:
            conn.enable_load_extension(True)

        applied = pragmas or DEFAULT_PRAGMAS
        for pragma in applied:
            conn.execute(pragma)

        if load_extension:
            conn.enable_load_extension(False)

        self.register(name, conn, path)
        return conn

    # ── registry ────────────────────────────────────────────────────────

    def register(self, name: str, conn: sqlite3.Connection, db_path: Path) -> None:
        """Track a named SQLite connection."""
        with self._lock:
            self._entries[name] = _PoolEntry(conn=conn, path=db_path)
            self._by_path.setdefault(db_path, []).append(name)
            log.debug("SQLite pool: registered '%s' → %s", name, db_path)

    def unregister(self, name: str) -> None:
        """Remove a connection from tracking (does NOT close it)."""
        with self._lock:
            entry = self._entries.pop(name, None)
            if entry is not None:
                names = self._by_path.get(entry.path)
                if names is not None:
                    try:
                        names.remove(name)
                    except ValueError:
                        pass
                    if not names:
                        self._by_path.pop(entry.path, None)

    # ── per-database queries ────────────────────────────────────────────

    def connections_for_db(self, db_path: str | Path) -> dict[str, str]:
        """Return tracked connection names for a specific database file."""
        path = Path(db_path)
        with self._lock:
            names = self._by_path.get(path, [])
            return {n: str(self._entries[n].path) for n in names if n in self._entries}

    @property
    def db_stats(self) -> dict[str, int]:
        """Connection count grouped by database file path."""
        with self._lock:
            return {str(p): len(ns) for p, ns in self._by_path.items() if ns}

    # ── lifecycle ───────────────────────────────────────────────────────

    def close_all(self) -> list[str]:
        """Close all tracked connections.  Returns names of closed connections.

        Idempotent — safe to call during shutdown even if individual
        components already closed their own connections (unregistered
        entries are gone from the pool by then).
        """
        with self._lock:
            names = list(self._entries.keys())
            entries = list(self._entries.values())
            self._entries.clear()
            self._by_path.clear()

        for entry in entries:
            try:
                entry.conn.close()
            except Exception:
                log_noncritical(
                    NonCriticalCategory.CONNECTION_CLEANUP,
                    f"Failed to close SQLite connection during pool shutdown: {entry.path}",
                    logger=log,
                )

        if names:
            log.info(
                "SQLite pool: closed %d connection(s): %s",
                len(names),
                ", ".join(names),
            )
        return names

    # ── monitoring ──────────────────────────────────────────────────────

    @property
    def active_connections(self) -> dict[str, str]:
        """Mapping of connection names to their database paths."""
        with self._lock:
            return {name: str(entry.path) for name, entry in self._entries.items()}

    @property
    def connection_count(self) -> int:
        """Number of currently tracked connections."""
        with self._lock:
            return len(self._entries)

    @property
    def max_connections(self) -> int:
        """Maximum pool capacity."""
        return self._max_connections

    @property
    def utilization(self) -> float:
        """Current pool utilization as fraction of max_connections (0.0–1.0)."""
        with self._lock:
            return len(self._entries) / self._max_connections


__all__ = ["SqliteConnectionPool", "DEFAULT_PRAGMAS"]
