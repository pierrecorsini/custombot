"""
Tests for src/db/sqlite_pool.py — SqliteConnectionPool idle reuse & LRU eviction.

Verifies:
- get_connection creates new connections when no idle match exists
- Idle connections are reused when requesting the same database path
- load_extension=True bypasses idle pool (extension loading is caller-specific)
- release_connection returns healthy connections to the idle pool
- release_connection closes unhealthy connections instead of idling them
- LRU eviction when idle pool exceeds max_idle
- close_all cleans up both active and idle connections
- Unhealthy idle connections are discarded during reuse
- Monitoring properties (idle_count, utilization) reflect pool state
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.db.sqlite_pool import SqliteConnectionPool


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def pool(tmp_path: Path) -> SqliteConnectionPool:
    """Provide a fresh pool with small limits for testing."""
    return SqliteConnectionPool(max_connections=10, max_idle=3)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Provide a temp database path."""
    return tmp_path / "test.db"


@pytest.fixture
def db_path_b(tmp_path: Path) -> Path:
    """Second database path for multi-database tests."""
    return tmp_path / "test_b.db"


# ─────────────────────────────────────────────────────────────────────────────
# Connection creation
# ─────────────────────────────────────────────────────────────────────────────


class TestGetConnection:
    """Verify get_connection creates and registers connections."""

    def test_creates_new_connection(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        conn = pool.get_connection("test", db_path)
        assert isinstance(conn, sqlite3.Connection)
        assert pool.connection_count == 1

    def test_applies_default_pragmas(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        conn = pool.get_connection("test", db_path)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_raises_at_capacity(self, db_path: Path) -> None:
        small_pool = SqliteConnectionPool(max_connections=2)
        small_pool.get_connection("a", db_path)
        small_pool.get_connection("b", db_path)

        with pytest.raises(sqlite3.OperationalError, match="at capacity"):
            small_pool.get_connection("c", db_path)

    def test_creates_parent_directory(self, pool: SqliteConnectionPool, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "deep.db"
        conn = pool.get_connection("deep", nested)
        assert isinstance(conn, sqlite3.Connection)


# ─────────────────────────────────────────────────────────────────────────────
# Idle connection reuse
# ─────────────────────────────────────────────────────────────────────────────


class TestIdleReuse:
    """Verify idle connections are reused when possible."""

    def test_reuses_idle_connection(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        conn_a = pool.get_connection("first", db_path)
        pool.release_connection("first")
        assert pool.idle_count == 1

        conn_b = pool.get_connection("second", db_path)
        assert conn_b is conn_a
        assert pool.idle_count == 0
        assert pool.connection_count == 1

    def test_creates_new_when_no_idle_match(
        self, pool: SqliteConnectionPool, db_path: Path, db_path_b: Path
    ) -> None:
        pool.get_connection("first", db_path)
        pool.release_connection("first")
        assert pool.idle_count == 1

        conn_b = pool.get_connection("second", db_path_b)
        assert conn_b is not None
        assert pool.idle_count == 1  # idle for db_path still there
        assert pool.connection_count == 1

    def test_load_extension_bypasses_idle(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        conn_a = pool.get_connection("first", db_path)
        pool.release_connection("first")
        assert pool.idle_count == 1

        conn_b = pool.get_connection("second", db_path, load_extension=True)
        assert conn_b is not conn_a
        assert pool.idle_count == 1  # idle conn still there (not popped)

    def test_discards_stale_idle_connection(
        self, pool: SqliteConnectionPool, db_path: Path
    ) -> None:
        conn_a = pool.get_connection("first", db_path)
        # Close the connection externally before releasing
        conn_a.close()

        pool.release_connection("first")
        # Unhealthy connection should NOT be in idle pool
        assert pool.idle_count == 0

    def test_discards_unhealthy_idle_during_reuse(
        self, pool: SqliteConnectionPool, db_path: Path
    ) -> None:
        conn_a = pool.get_connection("first", db_path)
        pool.release_connection("first")
        assert pool.idle_count == 1

        # Sabotage the idle connection by closing it externally
        # We need to get it from the idle pool directly
        with pool._lock:
            idle_conn = list(pool._idle.values())[0]
        idle_conn.close()

        # get_connection should detect the stale one and create a new connection
        conn_b = pool.get_connection("second", db_path)
        assert isinstance(conn_b, sqlite3.Connection)
        assert pool.connection_count == 1


# ─────────────────────────────────────────────────────────────────────────────
# Release & LRU eviction
# ─────────────────────────────────────────────────────────────────────────────


class TestReleaseAndEviction:
    """Verify release_connection and LRU eviction."""

    def test_release_returns_to_idle(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        pool.get_connection("test", db_path)
        pool.release_connection("test")
        assert pool.idle_count == 1
        assert pool.connection_count == 0

    def test_lru_eviction_when_over_max_idle(
        self, tmp_path: Path
    ) -> None:
        pool = SqliteConnectionPool(max_connections=10, max_idle=2)
        paths = [tmp_path / f"db_{i}.db" for i in range(4)]

        # Create and release 4 connections → only last 2 should be idle.
        for i, p in enumerate(paths):
            pool.get_connection(f"conn_{i}", p)
            pool.release_connection(f"conn_{i}")

        assert pool.idle_count == 2

    def test_release_unknown_name_is_noop(self, pool: SqliteConnectionPool) -> None:
        pool.release_connection("nonexistent")  # should not raise

    def test_release_unhealthy_conn_not_idled(
        self, pool: SqliteConnectionPool, db_path: Path
    ) -> None:
        conn = pool.get_connection("test", db_path)
        conn.close()  # make it unhealthy
        pool.release_connection("test")
        assert pool.idle_count == 0

    def test_lru_eviction_order(self, tmp_path: Path) -> None:
        """Least-recently-used idle connection is evicted first."""
        pool = SqliteConnectionPool(max_connections=10, max_idle=2)
        path_a = tmp_path / "a.db"
        path_b = tmp_path / "b.db"
        path_c = tmp_path / "c.db"

        # Release A, then B → idle has [A, B]
        pool.get_connection("a", path_a)
        pool.release_connection("a")
        pool.get_connection("b", path_b)
        pool.release_connection("b")

        # Release C → idle evicts A (LRU) → idle has [B, C]
        pool.get_connection("c", path_c)
        pool.release_connection("c")

        assert pool.idle_count == 2
        # B should still be in idle pool (was used more recently than A)
        conn_b = pool._try_reuse_idle(path_b.resolve())
        assert conn_b is not None
        conn_b.close()

        # Clean up
        pool.close_all()


# ─────────────────────────────────────────────────────────────────────────────
# close_all
# ─────────────────────────────────────────────────────────────────────────────


class TestCloseAll:
    """Verify close_all cleans up active + idle connections."""

    def test_closes_active_connections(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        conn = pool.get_connection("test", db_path)
        pool.close_all()
        # Connection should be closed by pool
        with pytest.raises(Exception):
            conn.execute("SELECT 1")

    def test_closes_idle_connections(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        conn = pool.get_connection("test", db_path)
        pool.release_connection("test")
        assert pool.idle_count == 1

        pool.close_all()
        assert pool.idle_count == 0
        assert pool.connection_count == 0
        # Connection should be closed
        with pytest.raises(Exception):
            conn.execute("SELECT 1")

    def test_idempotent(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        pool.get_connection("test", db_path)
        pool.close_all()
        pool.close_all()  # second call should not raise


# ─────────────────────────────────────────────────────────────────────────────
# Monitoring
# ─────────────────────────────────────────────────────────────────────────────


class TestMonitoring:
    """Verify monitoring properties."""

    def test_idle_count(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        assert pool.idle_count == 0
        pool.get_connection("test", db_path)
        assert pool.idle_count == 0
        pool.release_connection("test")
        assert pool.idle_count == 1

    def test_utilization(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        assert pool.utilization == 0.0
        pool.get_connection("test", db_path)
        assert pool.utilization == pytest.approx(0.1)  # 1/10

    def test_db_stats(self, pool: SqliteConnectionPool, db_path: Path, db_path_b: Path) -> None:
        pool.get_connection("a", db_path)
        pool.get_connection("b", db_path)
        pool.get_connection("c", db_path_b)

        stats = pool.db_stats
        assert stats[str(db_path.resolve())] == 2
        assert stats[str(db_path_b.resolve())] == 1

    def test_active_connections(self, pool: SqliteConnectionPool, db_path: Path) -> None:
        pool.get_connection("test", db_path)
        active = pool.active_connections
        assert "test" in active


# ─────────────────────────────────────────────────────────────────────────────
# Integration: release → reuse cycle
# ─────────────────────────────────────────────────────────────────────────────


class TestReuseCycle:
    """Verify full release → reuse lifecycle."""

    def test_repeated_open_close_reuses_connection(
        self, pool: SqliteConnectionPool, db_path: Path
    ) -> None:
        """Simulating component open/close cycles reuses idle connections."""
        created_ids: list[int] = []

        for i in range(3):
            name = f"worker_{i % 2}"
            conn = pool.get_connection(name, db_path)
            created_ids.append(id(conn))
            conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
            conn.commit()
            pool.release_connection(name)

        # The same underlying connection object should be reused
        assert created_ids[0] == created_ids[1] == created_ids[2]

    def test_capacity_accounts_for_active_only(
        self, tmp_path: Path
    ) -> None:
        """Idle connections don't consume active capacity slots."""
        pool = SqliteConnectionPool(max_connections=2, max_idle=5)
        paths = [tmp_path / f"db_{i}.db" for i in range(3)]

        # Create and release 3 connections → all should go idle
        for i, p in enumerate(paths):
            pool.get_connection(f"conn_{i}", p)
            pool.release_connection(f"conn_{i}")

        assert pool.idle_count == 3
        assert pool.connection_count == 0

        # Should still be able to create new connections (active cap = 2)
        pool.get_connection("new_a", paths[0])
        pool.get_connection("new_b", paths[1])
        assert pool.connection_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# Custom connection factory
# ─────────────────────────────────────────────────────────────────────────────


class TestCustomConnectionFactory:
    """Verify connection_factory parameter enables sqlcipher3-style backends."""

    def test_custom_factory_is_used(self, tmp_path: Path) -> None:
        """Pool delegates to the provided factory instead of sqlite3.connect."""
        calls: list[str] = []

        def tracking_factory(database: str, **kwargs: object) -> sqlite3.Connection:
            calls.append(database)
            return sqlite3.connect(database, **kwargs)

        pool = SqliteConnectionPool(connection_factory=tracking_factory)
        db_path = tmp_path / "tracked.db"
        pool.get_connection("test", db_path)

        assert len(calls) == 1
        assert str(db_path) in calls[0]

    def test_custom_factory_produces_working_connection(self, tmp_path: Path) -> None:
        """Connections from custom factory work normally (PRAGMAs, queries)."""
        pool = SqliteConnectionPool(connection_factory=sqlite3.connect)
        db_path = tmp_path / "custom.db"
        conn = pool.get_connection("test", db_path)

        # WAL mode should still be applied by the pool
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.commit()

    def test_default_factory_is_sqlite3_connect(self, tmp_path: Path) -> None:
        """Without explicit factory, sqlite3.connect is used (default behavior)."""
        pool = SqliteConnectionPool()
        db_path = tmp_path / "default.db"
        conn = pool.get_connection("test", db_path)
        assert isinstance(conn, sqlite3.Connection)

    def test_factory_receives_check_same_thread_false(self, tmp_path: Path) -> None:
        """Pool passes check_same_thread=False to the factory."""
        received_kwargs: dict[str, object] = {}

        def inspect_factory(database: str, **kwargs: object) -> sqlite3.Connection:
            received_kwargs.update(kwargs)
            return sqlite3.connect(database, **kwargs)

        pool = SqliteConnectionPool(connection_factory=inspect_factory)
        db_path = tmp_path / "kwargs.db"
        pool.get_connection("test", db_path)

        assert received_kwargs.get("check_same_thread") is False

    def test_idle_reuse_works_with_custom_factory(self, tmp_path: Path) -> None:
        """Idle connections from custom factory are reused correctly."""
        create_count = 0

        def counting_factory(database: str, **kwargs: object) -> sqlite3.Connection:
            nonlocal create_count
            create_count += 1
            return sqlite3.connect(database, **kwargs)

        pool = SqliteConnectionPool(connection_factory=counting_factory)
        db_path = tmp_path / "reuse.db"

        conn_a = pool.get_connection("first", db_path)
        pool.release_connection("first")

        conn_b = pool.get_connection("second", db_path)
        assert conn_b is conn_a
        assert create_count == 1  # Reused, not created again
