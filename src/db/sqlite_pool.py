"""
src/db/sqlite_pool.py — Bounded connection pool & factory for SQLite databases.

Provides:
- Bounded connection pool with configurable ``max_connections`` cap
- Idle connection reuse via LRU eviction (mirrors ``FileHandlePool``)
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
    # and release back to the idle pool on close().

    # At shutdown:
    pool.close_all()  # close any leaked or idle connections
"""

from __future__ import annotations

import logging
import sqlite3
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.constants.db import SQLITE_POOL_MAX_CONNECTIONS, SQLITE_POOL_MAX_IDLE_CONNECTIONS
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


def _close_conn(conn: sqlite3.Connection) -> None:
    """Close a SQLite connection, swallowing errors."""
    try:
        conn.close()
    except Exception:
        log_noncritical(
            NonCriticalCategory.CONNECTION_CLEANUP,
            "Failed to close SQLite connection",
            logger=log,
        )


def _is_alive(conn: sqlite3.Connection) -> bool:
    """Return True if the connection is usable (not closed and not stale)."""
    try:
        conn.execute("SELECT 1")
        return True
    except Exception:
        return False


class SqliteConnectionPool:
    """Bounded connection pool and factory for all SQLite databases.

    Centralizes connection creation, reuse, and lifecycle management so that:
    - All connections use consistent WAL-mode configuration
    - Idle connections are reused to reduce setup overhead
    - Total connection count is capped to limit file handle usage
    - Connections are grouped by database for monitoring and consistency
    - Graceful shutdown can close all connections in one step

    Idle connections are stored in an LRU ``OrderedDict`` keyed by database
    path.  When ``get_connection()`` is called, the idle pool is checked
    first; a matching connection is reused if healthy.  When a component
    releases a connection (via ``release_connection()``), it is returned to
    the idle pool for future reuse instead of being closed.

    Thread-safe via ThreadLock — safe for concurrent register/unregister
    from SqliteHelper subclasses running in different threads.
    """

    def __init__(
        self,
        *,
        max_connections: int = SQLITE_POOL_MAX_CONNECTIONS,
        max_idle: int = SQLITE_POOL_MAX_IDLE_CONNECTIONS,
    ) -> None:
        self._entries: dict[str, _PoolEntry] = {}
        self._by_path: dict[Path, list[str]] = {}
        self._max_connections = max_connections
        self._max_idle = max_idle
        self._idle: OrderedDict[Path, sqlite3.Connection] = OrderedDict()
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
        """Obtain a SQLite connection, reusing an idle one if available.

        Checks the idle pool for a matching database path first.  A healthy
        idle connection is reused (skipping directory creation, PRAGMAs, and
        connection setup).  If no idle connection is available, a new one is
        created with WAL mode and foreign keys enabled (or custom *pragmas*).

        Raises ``sqlite3.OperationalError`` when the pool is at capacity.

        Args:
            name: Unique identifier for this connection in the pool.
            db_path: Path to the database file.
            pragmas: Custom PRAGMAs (replaces defaults).  Ignored when
                reusing an idle connection (PRAGMAs already applied).
            load_extension: Enable extension loading before PRAGMAs.
                When True, idle connections are skipped because extension
                loading is caller-specific.

        Returns:
            Configured ``sqlite3.Connection``.
        """
        path = Path(db_path).resolve()

        # Try to reuse an idle connection (skip if caller needs extensions).
        if not load_extension:
            conn = self._try_reuse_idle(path)
            if conn is not None:
                self.register(name, conn, path)
                log.debug("SQLite pool: reused idle connection for '%s' → %s", name, path)
                return conn

        # Verify capacity before creating a new connection.
        with self._lock:
            if len(self._entries) >= self._max_connections:
                raise sqlite3.OperationalError(
                    f"SQLite pool at capacity ({self._max_connections} connections)"
                )

        path_unresolved = Path(db_path)
        path_unresolved.parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(str(path_unresolved), check_same_thread=False)

        if load_extension:
            conn.enable_load_extension(True)

        applied = pragmas or DEFAULT_PRAGMAS
        for pragma in applied:
            conn.execute(pragma)

        if load_extension:
            conn.enable_load_extension(False)

        self.register(name, conn, path)
        return conn

    def _try_reuse_idle(self, path: Path) -> Optional[sqlite3.Connection]:
        """Return a healthy idle connection for *path*, or None."""
        with self._lock:
            conn = self._idle.pop(path, None)
            if conn is None:
                return None

        # Validate outside the lock — fast health check.
        if _is_alive(conn):
            return conn

        # Stale — discard and keep looking.
        _close_conn(conn)
        return None

    # ── idle pool ───────────────────────────────────────────────────────

    def release_connection(self, name: str) -> None:
        """Release a connection back to the idle pool for reuse.

        Unregisters the connection from the active registry and places it
        in the idle LRU pool.  Connections are only added if they pass a
        health check; unhealthy connections are closed immediately.

        Idle entries are evicted (LRU) when the pool exceeds ``max_idle``.
        """
        with self._lock:
            entry = self._entries.pop(name, None)
            if entry is None:
                return

            # Remove from per-path index.
            names = self._by_path.get(entry.path)
            if names is not None:
                try:
                    names.remove(name)
                except ValueError:
                    pass
                if not names:
                    self._by_path.pop(entry.path, None)

            conn = entry.conn

            # Only keep healthy connections in the idle pool.
            if not _is_alive(conn):
                _close_conn(conn)
                return

            self._idle[entry.path] = conn
            self._idle.move_to_end(entry.path)

            # Evict LRU idle connections over capacity.
            while len(self._idle) > self._max_idle:
                _, evicted = self._idle.popitem(last=False)
                _close_conn(evicted)

        log.debug(
            "SQLite pool: released '%s' to idle pool (idle=%d, max_idle=%d)",
            name,
            len(self._idle),
            self._max_idle,
        )

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
        """Close all tracked and idle connections.  Returns names of closed connections.

        Idempotent — safe to call during shutdown even if individual
        components already closed their own connections (unregistered
        entries are gone from the pool by then).
        """
        with self._lock:
            names = list(self._entries.keys())
            entries = list(self._entries.values())
            self._entries.clear()
            self._by_path.clear()
            idle_conns = list(self._idle.values())
            self._idle.clear()

        for entry in entries:
            try:
                entry.conn.close()
            except Exception:
                log_noncritical(
                    NonCriticalCategory.CONNECTION_CLEANUP,
                    f"Failed to close SQLite connection during pool shutdown: {entry.path}",
                    logger=log,
                )

        for conn in idle_conns:
            _close_conn(conn)

        total = len(names) + len(idle_conns)
        if total:
            log.info(
                "SQLite pool: closed %d connection(s) (%d active, %d idle)",
                total,
                len(names),
                len(idle_conns),
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
        """Number of currently tracked (active) connections."""
        with self._lock:
            return len(self._entries)

    @property
    def idle_count(self) -> int:
        """Number of idle connections available for reuse."""
        with self._lock:
            return len(self._idle)

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
