"""DB retry budget helpers — lazy accessors for health-check integration.

Separated from ``db.py`` to avoid circular imports when
``check_performance_health`` needs DB budget state at module level.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.db.db import Database


def get_retry_budget_snapshot() -> dict[str, float] | None:
    """Return the current DB retry budget ratio and recovery ETA.

    Returns ``None`` when no Database instance has been registered
    (e.g. during early startup or in tests without a database).
    """
    db = _get_db()
    if db is None:
        return None
    return {
        "ratio": db.retry_budget_ratio,
        "recovery_eta_seconds": db.retry_budget_recovery_eta_seconds,
    }


# ── instance registry ────────────────────────────────────────────────────

_db_instance: Database | None = None


def register_db(db: Database) -> None:
    """Register the active Database instance for health-check polling."""
    global _db_instance
    _db_instance = db


def _get_db() -> Database | None:
    return _db_instance
