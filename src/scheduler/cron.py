"""scheduler/cron.py — Time-zone conversion and schedule-matching helpers.

Pure functions for UTC offset calculation, local-to-UTC time conversion,
and same-calendar-day comparison.  No class dependencies — used by
``engine.py`` for schedule evaluation.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = [
    "TICK_SECONDS",
    "_now",
    "_same_day",
    "_target_utc_time",
    "_utc_offset_hours",
]

TICK_SECONDS = 30  # check every 30s


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _utc_offset_hours() -> float:
    """Local UTC offset in hours (e.g. +1 for CET).

    .astimezone() converts a naive datetime to the local timezone,
    which makes .utcoffset() work reliably on all platforms (including Windows).
    """
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset else 0


def _target_utc_time(schedule: dict[str, float], local_offset: float) -> tuple[int, int]:
    """Convert a schedule's local ``hour:minute`` to UTC.

    Args:
        schedule: Dict with ``hour`` and ``minute`` keys (local time).
        local_offset: Local UTC offset in hours (e.g. +1 for CET).

    Returns:
        ``(utc_hour, utc_minute)`` tuple.
    """
    target_hour = schedule.get("hour", 0)
    target_min = schedule.get("minute", 0)
    local_total_min = target_hour * 60 + target_min
    utc_total_min = (local_total_min - int(local_offset * 60)) % (24 * 60)
    return utc_total_min // 60, utc_total_min % 60


def _same_day(iso_a: str, iso_b: str) -> bool:
    """Check if two ISO timestamps are on the same calendar day (UTC)."""
    try:
        da = datetime.fromisoformat(iso_a)
        db = datetime.fromisoformat(iso_b)
        return da.date() == db.date()
    except (ValueError, TypeError):
        return False
