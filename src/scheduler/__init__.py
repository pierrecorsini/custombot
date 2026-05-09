"""
src/scheduler — Async task scheduler package.

Provides:
  - TaskScheduler: Background scheduler that triggers bot actions on schedule
  - Cron helpers: _now, _utc_offset_hours, _target_utc_time, _same_day
  - Persistence: SCHEDULER_DIR, TASKS_FILE constants

Re-exports public symbols so that ``from src.scheduler import TaskScheduler``
and ``from src.scheduler import _now`` continue to work as before.
"""

from src.scheduler.cron import TICK_SECONDS, _now, _same_day, _target_utc_time, _utc_offset_hours
from src.scheduler.engine import TaskScheduler
from src.scheduler.persistence import SCHEDULER_DIR, TASKS_FILE

__all__ = [
    "TaskScheduler",
    "SCHEDULER_DIR",
    "TASKS_FILE",
    "TICK_SECONDS",
    "_now",
    "_same_day",
    "_target_utc_time",
    "_utc_offset_hours",
]
