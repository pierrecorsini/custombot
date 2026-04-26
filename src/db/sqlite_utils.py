"""
src/db/sqlite_utils.py — Shared SQLite connection utilities.

Provides consistent connection setup (PRAGMAs, thread safety)
for all SQLite-backed modules (ProjectStore, VectorMemory, etc.).

Usage:
    from src.db.sqlite_utils import get_sqlite_connection

    conn = get_sqlite_connection(db_path)
    # conn has WAL mode + foreign keys enabled
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Optional

from src.utils.locking import ThreadLock

log = logging.getLogger(__name__)

# Standard PRAGMAs applied to all connections
_DEFAULT_PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
]


def get_sqlite_connection(
    db_path: str | Path,
    *,
    pragmas: Optional[list[str]] = None,
    check_same_thread: bool = False,
) -> sqlite3.Connection:
    """
    Create a SQLite connection with standard configuration.

    Creates parent directories if needed, opens the connection with
    check_same_thread=False (safe when using external locking),
    and applies standard PRAGMAs (WAL mode, foreign keys).

    Args:
        db_path: Path to the database file.
        pragmas: Custom PRAGMAs to apply (replaces defaults).
        check_same_thread: Passed to sqlite3.connect (default False).

    Returns:
        Configured sqlite3.Connection.

    Example:
        conn = get_sqlite_connection("workspace/data.db")
        conn.execute("CREATE TABLE ...")
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path), check_same_thread=check_same_thread)

    applied_pragmas = pragmas or _DEFAULT_PRAGMAS
    for pragma in applied_pragmas:
        conn.execute(pragma)

    return conn


class SqliteHelper:
    """
    Mixin for thread-safe SQLite access with standard lifecycle.

    Provides connect(), close(), and thread-safe execute/commit helpers.
    Subclasses must set self._db_path before calling connect().

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

    def _open_connection(
        self,
        *,
        pragmas: Optional[list[str]] = None,
        load_extension: bool = False,
    ) -> sqlite3.Connection:
        """
        Open a SQLite connection with standard configuration.

        Args:
            pragmas: Custom PRAGMAs (replaces defaults).
            load_extension: If True, enable extension loading before PRAGMAs.

        Returns:
            The opened connection (also stored as self._db).
        """
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False)

        if load_extension:
            self._db.enable_load_extension(True)

        applied_pragmas = pragmas or _DEFAULT_PRAGMAS
        for pragma in applied_pragmas:
            self._db.execute(pragma)

        if load_extension:
            self._db.enable_load_extension(False)

        return self._db

    def close(self) -> None:
        """Close the database connection."""
        if self._db:
            self._db.close()
            self._db = None

    def _execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe DB execute — acquires lock for all DB operations."""
        assert self._db is not None
        with self._lock:
            return self._db.execute(sql, params)

    def _commit(self) -> None:
        """Thread-safe commit."""
        assert self._db is not None
        with self._lock:
            self._db.commit()

    def _execute_and_commit(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Thread-safe execute + commit in one lock acquisition."""
        assert self._db is not None
        with self._lock:
            cur = self._db.execute(sql, params)
            self._db.commit()
            return cur
