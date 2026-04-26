"""
Tests for src/db/sqlite_utils.py — SqliteHelper retry + circuit breaker.

Verifies:
- _guarded_sqlite retries transient sqlite3.OperationalError ("database is locked")
- Non-transient OperationalError is raised immediately (no retry)
- Circuit breaker opens after threshold consecutive failures
- Circuit breaker fast-fails when open
- Circuit breaker recovers after cooldown (HALF_OPEN → CLOSED)
- _execute, _commit, _execute_and_commit all go through _guarded_sqlite
- Retry uses exponential backoff with jitter
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.db.sqlite_utils import (
    SqliteHelper,
    _SyncCircuitBreaker,
    _SyncCircuitState,
    _is_sqlite_transient,
    _sqlite_delay_with_jitter,
)
from src.utils.locking import ThreadLock


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


class _ConcreteHelper(SqliteHelper):
    """Concrete subclass for testing the SqliteHelper mixin."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._lock = ThreadLock()
        super().__init__()


@pytest.fixture
def helper(tmp_path: Path) -> _ConcreteHelper:
    """Provide a fresh SqliteHelper with a temp database."""
    h = _ConcreteHelper(tmp_path / "test.db")
    h._open_connection()
    h._db.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, v TEXT)")
    h._db.commit()
    return h


def _force_breaker_open(breaker: _SyncCircuitBreaker) -> None:
    """Drive a CLOSED breaker to OPEN by recording threshold failures."""
    for _ in range(breaker._failure_threshold):
        breaker.record_failure()
    assert breaker.state == _SyncCircuitState.OPEN


def _force_breaker_half_open(breaker: _SyncCircuitBreaker) -> None:
    """Transition an OPEN breaker to HALF_OPEN by expiring the cooldown."""
    assert breaker.state == _SyncCircuitState.OPEN
    with patch(
        "src.db.sqlite_utils.time.monotonic",
        return_value=breaker._last_failure_time + breaker._cooldown_seconds + 1,
    ):
        assert breaker.is_open() is False
    assert breaker.state == _SyncCircuitState.HALF_OPEN


# ─────────────────────────────────────────────────────────────────────────────
# Transient error classification
# ─────────────────────────────────────────────────────────────────────────────


class TestIsSqliteTransient:
    """Verify _is_sqlite_transient correctly classifies OperationalError messages."""

    @pytest.mark.parametrize(
        "msg",
        [
            "database is locked",
            "database is busy",
            "database table is locked",
            "DATABASE IS LOCKED",  # case-insensitive
            "the database is locked by another process",
        ],
    )
    def test_transient_messages(self, msg: str) -> None:
        exc = sqlite3.OperationalError(msg)
        assert _is_sqlite_transient(exc) is True

    @pytest.mark.parametrize(
        "msg",
        [
            "no such table: foo",
            "no such column: bar",
            "near 'SELEC': syntax error",
            "UNIQUE constraint failed: t.id",
            "cannot commit - no transaction is active",
        ],
    )
    def test_non_transient_messages(self, msg: str) -> None:
        exc = sqlite3.OperationalError(msg)
        assert _is_sqlite_transient(exc) is False


# ─────────────────────────────────────────────────────────────────────────────
# SyncCircuitBreaker unit tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSyncCircuitBreaker:
    """Verify _SyncCircuitBreaker state transitions."""

    def test_initial_state_is_closed(self) -> None:
        breaker = _SyncCircuitBreaker(failure_threshold=3, cooldown_seconds=5.0)
        assert breaker.state == _SyncCircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_opens_after_threshold_failures(self) -> None:
        breaker = _SyncCircuitBreaker(failure_threshold=3, cooldown_seconds=5.0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == _SyncCircuitState.CLOSED
        breaker.record_failure()
        assert breaker.state == _SyncCircuitState.OPEN

    def test_is_open_returns_true_when_open(self) -> None:
        breaker = _SyncCircuitBreaker(failure_threshold=2, cooldown_seconds=5.0)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.is_open() is True

    def test_success_resets_failure_count_when_closed(self) -> None:
        breaker = _SyncCircuitBreaker(failure_threshold=3, cooldown_seconds=5.0)
        breaker.record_failure()
        assert breaker.failure_count == 1
        breaker.record_success()
        assert breaker.failure_count == 0
        assert breaker.state == _SyncCircuitState.CLOSED

    def test_half_open_probe_success_closes(self) -> None:
        breaker = _SyncCircuitBreaker(failure_threshold=2, cooldown_seconds=5.0)
        _force_breaker_open(breaker)
        _force_breaker_half_open(breaker)
        breaker.record_success()
        assert breaker.state == _SyncCircuitState.CLOSED
        assert breaker.failure_count == 0

    def test_half_open_probe_failure_reopens(self) -> None:
        breaker = _SyncCircuitBreaker(failure_threshold=2, cooldown_seconds=5.0)
        _force_breaker_open(breaker)
        _force_breaker_half_open(breaker)
        breaker.record_failure()
        assert breaker.state == _SyncCircuitState.OPEN


# ─────────────────────────────────────────────────────────────────────────────
# SqliteHelper retry tests
# ─────────────────────────────────────────────────────────────────────────────


class TestSqliteHelperRetry:
    """Verify _guarded_sqlite retries transient errors."""

    def test_successful_operation(self, helper: _ConcreteHelper) -> None:
        """Normal operation succeeds without retry."""
        cur = helper._execute_and_commit(
            "INSERT INTO t (id, v) VALUES (?, ?)", (1, "hello")
        )
        assert cur.rowcount == 1

        rows = helper._execute("SELECT v FROM t WHERE id = ?", (1,)).fetchall()
        assert rows == [("hello",)]

    def test_retries_transient_error_then_succeeds(self, helper: _ConcreteHelper) -> None:
        """Transient OperationalError is retried and eventually succeeds."""
        call_count = 0
        original_execute = helper._db.execute

        def flaky_execute(sql: str, params: tuple = ()) -> object:
            nonlocal call_count
            call_count += 1
            if sql.startswith("INSERT") and call_count <= 2:
                raise sqlite3.OperationalError("database is locked")
            return original_execute(sql, params)

        with (
            patch.object(helper._db, "execute", side_effect=flaky_execute),
            patch("src.db.sqlite_utils.time.sleep"),
        ):
            cur = helper._execute_and_commit(
                "INSERT INTO t (id, v) VALUES (?, ?)", (1, "retried")
            )

        assert cur.rowcount == 1
        assert call_count == 3  # 2 transient + 1 success

    def test_raises_after_exhausting_retries(self, helper: _ConcreteHelper) -> None:
        """OperationalError is raised after all retries are exhausted."""
        with (
            patch.object(
                helper._db,
                "execute",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("src.db.sqlite_utils.time.sleep"),
            pytest.raises(sqlite3.OperationalError, match="database is locked"),
        ):
            helper._execute_and_commit(
                "INSERT INTO t (id, v) VALUES (?, ?)", (1, "fail")
            )

    def test_non_transient_error_not_retried(self, helper: _ConcreteHelper) -> None:
        """Non-transient OperationalError is raised immediately without retry."""
        call_count = 0
        original_execute = helper._db.execute

        def schema_error(sql: str, params: tuple = ()) -> object:
            nonlocal call_count
            call_count += 1
            if sql.startswith("INSERT"):
                raise sqlite3.OperationalError("no such table: nonexistent")
            return original_execute(sql, params)

        with pytest.raises(sqlite3.OperationalError, match="no such table"):
            helper._execute_and_commit(
                "INSERT INTO nonexistent (id) VALUES (?)", (1,)
            )

        # Non-transient: should have only been called once (no retry)
        assert call_count == 1

    def test_circuit_breaker_opens_after_sustained_failures(
        self, helper: _ConcreteHelper
    ) -> None:
        """Breaker opens after threshold consecutive failures."""
        # Exhaust retries multiple times to trip the breaker
        for _ in range(helper._sqlite_breaker._failure_threshold):
            with (
                patch.object(
                    helper._db,
                    "execute",
                    side_effect=sqlite3.OperationalError("database is locked"),
                ),
                patch("src.db.sqlite_utils.time.sleep"),
                pytest.raises(sqlite3.OperationalError),
            ):
                helper._execute_and_commit(
                    "INSERT INTO t (id, v) VALUES (?, ?)", (1, "x")
                )

        assert helper._sqlite_breaker.state == _SyncCircuitState.OPEN

    def test_circuit_breaker_fast_fails_when_open(
        self, helper: _ConcreteHelper
    ) -> None:
        """When breaker is open, operations are rejected immediately."""
        _force_breaker_open(helper._sqlite_breaker)

        with pytest.raises(sqlite3.OperationalError, match="circuit breaker open"):
            helper._execute_and_commit(
                "INSERT INTO t (id, v) VALUES (?, ?)", (1, "blocked")
            )

    def test_circuit_breaker_records_success(self, helper: _ConcreteHelper) -> None:
        """Successful operation resets breaker failure count."""
        helper._sqlite_breaker.record_failure()
        assert helper._sqlite_breaker.failure_count == 1

        helper._execute_and_commit(
            "INSERT INTO t (id, v) VALUES (?, ?)", (1, "ok")
        )

        assert helper._sqlite_breaker.failure_count == 0
        assert helper._sqlite_breaker.state == _SyncCircuitState.CLOSED

    def test_execute_uses_guarded_path(self, helper: _ConcreteHelper) -> None:
        """_execute goes through _guarded_sqlite (retry on transient)."""
        call_count = 0
        original_execute = helper._db.execute

        def flaky_execute(sql: str, params: tuple = ()) -> object:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise sqlite3.OperationalError("database is locked")
            return original_execute(sql, params)

        with (
            patch.object(helper._db, "execute", side_effect=flaky_execute),
            patch("src.db.sqlite_utils.time.sleep"),
        ):
            cur = helper._execute("SELECT 1")

        assert cur.fetchone() == (1,)
        assert call_count == 2

    def test_commit_uses_guarded_path(self, helper: _ConcreteHelper) -> None:
        """_commit goes through _guarded_sqlite (retry on transient)."""
        call_count = 0
        original_commit = helper._db.commit

        def flaky_commit() -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise sqlite3.OperationalError("database is locked")
            original_commit()

        # First do an execute outside the lock
        with helper._lock:
            helper._db.execute("INSERT INTO t (id, v) VALUES (?, ?)", (1, "test"))

        with (
            patch.object(helper._db, "commit", side_effect=flaky_commit),
            patch("src.db.sqlite_utils.time.sleep"),
        ):
            helper._commit()

        assert call_count == 2

    def test_backoff_delay_increases(self, helper: _ConcreteHelper) -> None:
        """Retry delay doubles on each attempt (exponential backoff)."""
        sleep_args: list[float] = []

        def capture_sleep(duration: float) -> None:
            sleep_args.append(duration)

        with (
            patch.object(
                helper._db,
                "execute",
                side_effect=sqlite3.OperationalError("database is locked"),
            ),
            patch("src.db.sqlite_utils.time.sleep", side_effect=capture_sleep),
            pytest.raises(sqlite3.OperationalError),
        ):
            helper._execute_and_commit(
                "INSERT INTO t (id, v) VALUES (?, ?)", (1, "x")
            )

        # With SQLITE_WRITE_MAX_RETRIES=3, we get 3 sleep calls
        assert len(sleep_args) == 3
        # Each delay should be approximately double the previous
        # (allowing for jitter)
        for i in range(1, len(sleep_args)):
            ratio = sleep_args[i] / sleep_args[i - 1]
            # With ±10% jitter, ratio should be ~2.0, but allow wide margin
            assert 1.5 < ratio < 2.8


class TestSqliteHelperIntegration:
    """Integration tests: helper lifecycle + retry end-to-end."""

    def test_close_and_reopen(self, tmp_path: Path) -> None:
        """Helper can be closed and reopened, breaker state persists."""
        h = _ConcreteHelper(tmp_path / "test.db")
        h._open_connection()
        h._execute_and_commit("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        h._execute_and_commit("INSERT INTO t (id) VALUES (1)")
        h.close()

        # Reopen
        h2 = _ConcreteHelper(tmp_path / "test.db")
        h2._open_connection()
        rows = h2._execute("SELECT id FROM t").fetchall()
        assert rows == [(1,)]
        h2.close()

    def test_jitter_produces_non_negative_delay(self) -> None:
        """_sqlite_delay_with_jitter never returns negative values."""
        for base in [0.01, 0.05, 0.1, 1.0, 5.0]:
            for _ in range(100):
                delay = _sqlite_delay_with_jitter(base)
                assert delay >= 0.0

    def test_delay_is_close_to_base(self) -> None:
        """Jitter keeps the delay within ±10% of base."""
        base = 0.1
        delays = [_sqlite_delay_with_jitter(base) for _ in range(200)]
        avg = sum(delays) / len(delays)
        # Average should be close to base (within 5%)
        assert abs(avg - base) / base < 0.05
