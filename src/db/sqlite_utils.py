"""
src/db/sqlite_utils.py — Shared SQLite connection utilities.

Provides consistent connection setup (PRAGMAs, thread safety)
for all SQLite-backed modules (ProjectStore, VectorMemory, etc.).

Includes retry-with-backoff and circuit-breaker protection for
transient ``sqlite3.OperationalError`` (database is locked / busy)
in :class:`SqliteHelper` write methods.

Usage:
    from src.db.sqlite_utils import get_sqlite_connection

    conn = get_sqlite_connection(db_path)
    # conn has WAL mode + foreign keys enabled
"""

from __future__ import annotations

import enum
import logging
import random
import sqlite3
import time
from pathlib import Path
from typing import ClassVar, Optional, TYPE_CHECKING

from src.constants import (
    SQLITE_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    SQLITE_WRITE_CIRCUIT_FAILURE_THRESHOLD,
    SQLITE_WRITE_MAX_RETRIES,
    SQLITE_WRITE_RETRY_INITIAL_DELAY,
)
from src.db.sqlite_pool import DEFAULT_PRAGMAS as _DEFAULT_PRAGMAS
from src.db.sqlite_pool import ConnectionFactory as _ConnectionFactory
from src.utils.locking import ThreadLock

if TYPE_CHECKING:
    from src.db.sqlite_pool import SqliteConnectionPool

log = logging.getLogger(__name__)


def get_sqlite_connection(
    db_path: str | Path,
    *,
    pragmas: Optional[list[str]] = None,
    check_same_thread: bool = False,
    connection_factory: _ConnectionFactory | None = None,
) -> sqlite3.Connection:
    """
    Create a SQLite connection with standard configuration.

    Creates parent directories if needed, opens the connection with
    check_same_thread=False (safe when using external locking),
    and applies standard PRAGMAs (WAL mode, foreign keys).

    Args:
        db_path: Path to the database file.
        pragmas: Custom PRAGMAs to apply (replaces defaults).
        check_same_thread: Passed to the connection factory (default False).
        connection_factory: Custom connection factory callable
            (e.g. ``sqlcipher3.connect``).  Defaults to ``sqlite3.connect``.

    Returns:
        Configured sqlite3.Connection.

    Example:
        conn = get_sqlite_connection("workspace/data.db")
        conn.execute("CREATE TABLE ...")
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    factory = connection_factory or sqlite3.connect
    conn = factory(str(path), check_same_thread=check_same_thread)

    applied_pragmas = pragmas or _DEFAULT_PRAGMAS
    for pragma in applied_pragmas:
        conn.execute(pragma)

    return conn


# ── Sync Circuit Breaker ───────────────────────────────────────────────


class _SyncCircuitState(str, enum.Enum):
    """Circuit breaker states (synchronous variant)."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _SyncCircuitBreaker:
    """Thread-safe synchronous circuit breaker for SQLite operations.

    After *failure_threshold* consecutive failures the breaker opens and
    rejects all calls for *cooldown_seconds*.  After the cooldown it
    transitions to HALF_OPEN and allows a single probe call.  A successful
    probe closes the breaker; a failed probe re-opens it.

    Uses ``threading.Lock`` (via ``ThreadLock``) for thread safety — safe
    for use in synchronous ``SqliteHelper`` methods.
    """

    def __init__(
        self,
        failure_threshold: int = SQLITE_WRITE_CIRCUIT_FAILURE_THRESHOLD,
        cooldown_seconds: float = SQLITE_WRITE_CIRCUIT_COOLDOWN_SECONDS,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state: _SyncCircuitState = _SyncCircuitState.CLOSED
        self._failure_count: int = 0
        self._last_failure_time: float = 0.0
        self._lock = ThreadLock()

    @property
    def state(self) -> _SyncCircuitState:
        """Current breaker state (for diagnostics / health endpoints)."""
        return self._state

    @property
    def failure_count(self) -> int:
        """Current consecutive failure count."""
        return self._failure_count

    def is_open(self) -> bool:
        """Return ``True`` when the breaker is OPEN and should reject calls.

        Side-effect: transitions from OPEN → HALF_OPEN once the cooldown
        elapses, so the *next* call is allowed as a probe.
        """
        with self._lock:
            if self._state == _SyncCircuitState.CLOSED:
                return False
            if self._state == _SyncCircuitState.HALF_OPEN:
                return False
            # OPEN — check cooldown
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self._cooldown_seconds:
                self._state = _SyncCircuitState.HALF_OPEN
                log.info(
                    "SQLite circuit breaker transitioned to HALF_OPEN after %.1fs cooldown",
                    elapsed,
                )
                return False
            return True

    def record_success(self) -> None:
        """Record a successful call.  Closes the breaker from HALF_OPEN."""
        with self._lock:
            if self._state == _SyncCircuitState.HALF_OPEN:
                self._state = _SyncCircuitState.CLOSED
                self._failure_count = 0
                log.info("SQLite circuit breaker CLOSED — probe succeeded")
            elif self._state == _SyncCircuitState.CLOSED:
                self._failure_count = 0

    def record_failure(self) -> None:
        """Record a failed call.  Opens the breaker when threshold is hit."""
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()

            if self._state == _SyncCircuitState.HALF_OPEN:
                self._state = _SyncCircuitState.OPEN
                log.warning("SQLite circuit breaker re-OPENED — probe failed")
            elif self._failure_count >= self._failure_threshold:
                self._state = _SyncCircuitState.OPEN
                log.warning(
                    "SQLite circuit breaker OPENED — %d consecutive failures (cooldown=%.0fs)",
                    self._failure_count,
                    self._cooldown_seconds,
                )


# ── SQLite Transient Error Detection ───────────────────────────────────

# Substrings in sqlite3.OperationalError messages that indicate transient
# lock contention (retriable) rather than schema/logic errors (permanent).
_SQLITE_TRANSIENT_PATTERNS = (
    "database is locked",
    "database is busy",
    "database table is locked",
)


def _is_sqlite_transient(error: sqlite3.OperationalError) -> bool:
    """Return True if the OperationalError is a transient lock-contention failure."""
    msg = str(error).lower()
    return any(pattern in msg for pattern in _SQLITE_TRANSIENT_PATTERNS)


def _sqlite_delay_with_jitter(base_delay: float) -> float:
    """Apply ±10% jitter to *base_delay* to prevent thundering herd."""
    jitter = base_delay * 0.1 * random.uniform(-1, 1)
    return max(0.0, base_delay + jitter)


# ── SqliteHelper ───────────────────────────────────────────────────────


class SqliteHelper:
    """
    Mixin for thread-safe SQLite access with standard lifecycle.

    Provides connect(), close(), and thread-safe execute/commit helpers
    with automatic retry-with-backoff and circuit-breaker protection for
    transient ``sqlite3.OperationalError`` (database is locked / busy).

    Subclasses must set ``self._db_path`` before calling ``connect()``.

    Usage:
        class MyStore(SqliteHelper):
            def __init__(self, db_path: str):
                self._db_path = Path(db_path)
                self._lock = ThreadLock()
                super().__init__()

            def connect(self):
                self._open_connection()
                self._db.execute("CREATE TABLE IF NOT EXISTS ...")
                self._db.commit()
    """

    _db: Optional[sqlite3.Connection] = None
    _db_path: Path
    _lock: ThreadLock
    _sqlite_breaker: _SyncCircuitBreaker

    # Class-level pool shared by all SqliteHelper instances.
    _pool: ClassVar["SqliteConnectionPool | None"] = None

    def __init__(self, *args: object, **kwargs: object) -> None:
        self._sqlite_breaker = _SyncCircuitBreaker()
        super().__init__(*args, **kwargs)

    @classmethod
    def set_pool(cls, pool: "SqliteConnectionPool") -> None:
        """Set the shared connection pool for all SqliteHelper instances."""
        cls._pool = pool

    @classmethod
    def close_all_connections(cls) -> list[str]:
        """Close all tracked connections via the pool (safety net for shutdown)."""
        if cls._pool is not None:
            return cls._pool.close_all()
        return []

    def _open_connection(
        self,
        *,
        pragmas: Optional[list[str]] = None,
        load_extension: bool = False,
        connection_factory: _ConnectionFactory | None = None,
    ) -> sqlite3.Connection:
        """
        Open a SQLite connection with standard configuration.

        When a pool is set (via :meth:`set_pool`), delegates connection
        creation to the pool factory for consistent WAL-mode configuration
        and centralized lifecycle management.  Otherwise falls back to
        creating the connection directly using *connection_factory*
        (defaults to ``sqlite3.connect``).

        Args:
            pragmas: Custom PRAGMAs (replaces defaults).
            load_extension: If True, enable extension loading before PRAGMAs.
            connection_factory: Custom connection factory callable
                (e.g. ``sqlcipher3.connect``).  Defaults to ``sqlite3.connect``.
                Ignored when the pool is configured — the pool's factory is used.

        Returns:
            The opened connection (also stored as self._db).
        """
        name = f"{type(self).__qualname__}"

        if SqliteHelper._pool is not None:
            self._db = SqliteHelper._pool.get_connection(
                name=name,
                db_path=self._db_path,
                pragmas=pragmas,
                load_extension=load_extension,
            )
            return self._db

        # Fallback: create connection directly (no pool configured).
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        factory = connection_factory or sqlite3.connect
        self._db = factory(str(self._db_path), check_same_thread=False)

        if load_extension:
            self._db.enable_load_extension(True)

        applied_pragmas = pragmas or _DEFAULT_PRAGMAS
        for pragma in applied_pragmas:
            self._db.execute(pragma)

        if load_extension:
            self._db.enable_load_extension(False)

        return self._db

    def close(self) -> None:
        """Close the database connection.

        When a pool is configured, the connection is released back to the
        pool's idle pool for reuse instead of being closed immediately.
        """
        if self._db:
            if SqliteHelper._pool is not None:
                SqliteHelper._pool.release_connection(f"{type(self).__qualname__}")
            else:
                self._db.close()
            self._db = None

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe DB execute with retry on transient lock errors."""
        assert self._db is not None
        return self._guarded_sqlite(lambda: self._db.execute(sql, params))  # type: ignore[union-attr]

    def _commit(self) -> None:
        """Thread-safe commit with retry on transient lock errors."""
        assert self._db is not None
        self._guarded_sqlite(lambda: self._db.commit())  # type: ignore[union-attr]

    def _execute_and_commit(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe execute + commit with retry on transient lock errors."""
        assert self._db is not None

        def _op() -> sqlite3.Cursor:
            cur = self._db.execute(sql, params)  # type: ignore[union-attr]
            self._db.commit()  # type: ignore[union-attr]
            return cur

        return self._guarded_sqlite(_op)

    def _guarded_sqlite(self, operation: object) -> object:
        """Execute a SQLite operation with retry + circuit-breaker protection.

        Retries transient ``sqlite3.OperationalError`` (database is locked /
        busy) with exponential backoff + jitter.  Only after all retries are
        exhausted is a failure recorded on the circuit breaker.

        Args:
            operation: Zero-arg callable performing the SQLite operation.

        Returns:
            The return value of *operation*.

        Raises:
            sqlite3.OperationalError: If all retries are exhausted or the
                error is non-transient (e.g., schema error).
        """
        if self._sqlite_breaker.is_open():
            raise sqlite3.OperationalError("SQLite write circuit breaker open — operation rejected")

        delay = SQLITE_WRITE_RETRY_INITIAL_DELAY

        for attempt in range(SQLITE_WRITE_MAX_RETRIES + 1):
            try:
                with self._lock:
                    result = operation()
                self._sqlite_breaker.record_success()
                return result
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_transient(exc):
                    # Schema/logic error — never retry
                    raise

                if attempt >= SQLITE_WRITE_MAX_RETRIES:
                    log.warning(
                        "SQLite retry exhausted after %d attempts: %s",
                        SQLITE_WRITE_MAX_RETRIES + 1,
                        exc,
                    )
                    self._sqlite_breaker.record_failure()
                    raise

                actual_delay = _sqlite_delay_with_jitter(delay)
                log.info(
                    "Transient SQLite error, retrying attempt %d/%d after %.3fs: %s",
                    attempt + 1,
                    SQLITE_WRITE_MAX_RETRIES,
                    actual_delay,
                    exc,
                )
                time.sleep(actual_delay)
                delay *= 2


__all__ = [
    "SqliteHelper",
    "get_sqlite_connection",
    "_SyncCircuitBreaker",
    "_SyncCircuitState",
]
