"""
src/security/audit.py — Structured audit logging for security-relevant events.

Provides:
  - ``audit_log``: in-memory structured log for security events
  - ``SkillAuditLogger``: persistent JSONL audit trail for skill executions
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.locking import ThreadLockMixin

log = logging.getLogger("security.audit")


# ── In-memory structured audit ───────────────────────────────────────────


def audit_log(
    event: str,
    details: dict[str, Any],
    *,
    level: int = logging.WARNING,
    prefix: str = "AUDIT",
) -> None:
    """
    Log a structured audit event.

    Args:
        event: Event type (e.g., "file_read", "command_blocked", "path_blocked").
        details: Additional context about the event.
        level: Logging level (default WARNING for security events).
        prefix: Prefix for the log message (e.g., "FILE_AUDIT", "SECURITY_AUDIT").
    """
    log.log(
        level,
        "%s: %s | %s",
        prefix,
        event,
        " | ".join(f"{k}={v}" for k, v in details.items()),
        extra={f"audit_{prefix.lower()}": event, **details},
    )


# ── Persistent skill-audit JSONL logger ─────────────────────────────────


class SkillAuditLogger(ThreadLockMixin):
    """Persistent JSONL audit trail for skill executions.

    Appends one JSON line per skill execution to
    ``<log_dir>/audit.jsonl`` with fields::

        timestamp, chat_id, skill_name, args_hash, allowed, result_summary

    Thread-safe via ``ThreadLock`` (see src.utils.locking).  Rotates when the
    current file exceeds ``MAX_FILE_SIZE_BYTES``, keeping up to
    ``MAX_ROTATED_FILES`` historical copies.
    """

    MAX_FILE_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB
    MAX_ROTATED_FILES: int = 5
    _CHAIN_KEY: bytes = b"custombot.audit.chain.v1"

    def __init__(self, log_dir: str | Path, *, chain_hashes: bool = False) -> None:
        super().__init__()
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "audit.jsonl"
        self._chain_hashes = chain_hashes
        self._prev_hash: str | None = (
            hmac.new(self._CHAIN_KEY, b"", hashlib.sha256).hexdigest()
            if chain_hashes
            else None
        )

    # ── public API ───────────────────────────────────────────────────────

    def log(
        self,
        chat_id: str,
        skill_name: str,
        args_hash: str,
        allowed: bool,
        result_summary: str,
    ) -> None:
        """Append a single audit entry (thread-safe)."""
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "chat_id": chat_id,
            "skill_name": skill_name,
            "args_hash": args_hash,
            "allowed": allowed,
            "result_summary": result_summary,
        }
        if self._prev_hash is not None:
            entry["_prev_hash"] = self._prev_hash
        line = json.dumps(entry, default=str)
        if self._prev_hash is not None:
            self._prev_hash = hmac.new(
                self._CHAIN_KEY, line.encode("utf-8"), hashlib.sha256
            ).hexdigest()
        with self._lock:
            if self._path is None:
                return  # logger has been closed
            try:
                with open(self._path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                self._maybe_rotate()
            except OSError as exc:
                log.warning("Failed to write audit entry: %s", exc)

    @staticmethod
    def hash_args(raw_args: str) -> str:
        """Return a truncated SHA-256 hex digest of *raw_args* (128 bits)."""
        return hashlib.sha256(raw_args.encode("utf-8")).hexdigest()[:32]

    # ── rotation ─────────────────────────────────────────────────────────

    def _maybe_rotate(self) -> None:
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self.MAX_FILE_SIZE_BYTES:
            return
        # Shift rotated files: audit.{i}.jsonl → audit.{i+1}.jsonl
        for i in range(self.MAX_ROTATED_FILES - 1, 0, -1):
            src = self._dir / f"audit.{i}.jsonl"
            dst = self._dir / f"audit.{i + 1}.jsonl"
            if src.exists():
                src.rename(dst)
        self._path.rename(self._dir / "audit.1.jsonl")

    # ── lifecycle ─────────────────────────────────────────────────────────

    def close(self) -> None:
        """Flush and release audit-logger resources.

        Safe to call multiple times or when the logger was never used.
        """
        # SkillAuditLogger opens/closes files per-write via ``with open(...)``,
        # so there are no lingering handles.  Release the lock and path
        # references so subsequent ``log()`` calls become no-ops.
        with self._lock:
            self._path = None  # type: ignore[assignment]
            self._dir = None  # type: ignore[assignment]
        log.debug("SkillAuditLogger closed")

    # ── TTL cleanup ──────────────────────────────────────────────────────

    def cleanup_old_logs(
        self,
        max_age_days: int,
        max_files: int,
    ) -> int:
        """Remove rotated audit files older than *max_age_days* or exceeding *max_files*.

        Called periodically by the ``WorkspaceMonitor`` cleanup cycle to
        prevent unbounded disk growth.  Returns the number of files removed.
        """
        cutoff = time.time() - (max_age_days * 86400)
        pruned = 0

        # Collect rotated audit files (audit.{i}.jsonl)
        try:
            rotated = sorted(
                (
                    f
                    for f in self._dir.iterdir()
                    if f.is_file()
                    and f.name.startswith("audit.")
                    and f.name.endswith(".jsonl")
                    and f.name != "audit.jsonl"
                ),
                key=lambda f: f.stat().st_mtime,
            )
        except OSError:
            return 0

        # Age-based pruning
        for f in list(rotated):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    rotated.remove(f)
                    pruned += 1
                    log.debug("Pruned aged audit log: %s", f.name)
            except OSError:
                pass

        # Count-based pruning: remove oldest files exceeding the limit
        while len(rotated) > max_files:
            oldest = rotated.pop(0)
            try:
                oldest.unlink()
                pruned += 1
                log.debug("Pruned excess audit log: %s", oldest.name)
            except OSError:
                pass

        if pruned > 0:
            log.info("Pruned %d audit log file(s)", pruned)
        return pruned
