"""
src/lifecycle_persistence.py — Persist AppPhase transitions to disk.

Enables crash recovery by recording the last-known lifecycle phase
in ``workspace/.data/phase.json``.  On startup, if the persisted
phase is ``RUNNING``, the application knows it crashed (clean
shutdown writes ``STOPPED``).

Usage::

    from src.lifecycle_persistence import PhasePersistence
    from src.app import AppPhase

    persist = PhasePersistence()
    previous = persist.load_phase()
    if previous == AppPhase.RUNNING:
        log.warning("Crash recovery: previous session did not shut down cleanly")
    persist.save_phase(AppPhase.STARTING)
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from src.constants import WORKSPACE_DIR

log = logging.getLogger(__name__)

_PHASE_FILE = Path(WORKSPACE_DIR) / ".data" / "phase.json"


class PhasePersistence:
    """Persist and retrieve the last-known application lifecycle phase.

    Phase is stored as a simple JSON file ``{"phase": "RUNNING"}``.
    All methods are synchronous because they run during early startup
    (before the event loop is available) or during final shutdown.
    """

    def __init__(self, path: Path = _PHASE_FILE) -> None:
        self._path = path

    def save_phase(self, phase: "AppPhase") -> None:
        """Write the current phase to disk."""
        from src.app import AppPhase

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps({"phase": phase.name}), encoding="utf-8"
            )
        except OSError:
            log.debug("Failed to persist phase %s", phase.name, exc_info=True)

    def load_phase(self) -> "Optional[AppPhase]":
        """Read the last persisted phase, or ``None`` if unavailable."""
        from src.app import AppPhase

        if not self._path.exists():
            return None

        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.debug("Failed to load persisted phase", exc_info=True)
            return None

        name = data.get("phase")
        if not isinstance(name, str):
            return None

        try:
            return AppPhase[name]
        except KeyError:
            log.warning("Unknown persisted phase %r — ignoring", name)
            return None

    def save_stopped(self) -> None:
        """Convenience: write STOPPED phase on clean shutdown."""
        from src.app import AppPhase

        self.save_phase(AppPhase.STOPPED)
