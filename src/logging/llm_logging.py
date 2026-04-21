"""
src/logging/llm_logging.py — Per-file LLM request/response logging.

When enabled, each LLM call produces two JSON files in workspace/logs/llm/:

  {timestamp}_request_{request_id}.json   — full request payload
  {timestamp}_response_{request_id}.json  — full response payload

Both files share the same request_id so they can be paired.

Rotation:
  - Cleanup runs every ``LLM_LOG_CLEANUP_INTERVAL`` writes.
  - Removes files older than ``max_age_days``.
  - Keeps at most ``max_files`` files (oldest deleted first).
  - Log directory size is queryable via ``get_log_dir_size()``.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.constants import (
    LLM_LOG_CLEANUP_INTERVAL,
    LLM_LOG_MAX_AGE_DAYS,
    LLM_LOG_MAX_FILES,
)

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Secret redaction for log payloads
# ─────────────────────────────────────────────────────────────────────────────

_REDACTED = "[REDACTED]"

# Dict keys whose values should always be redacted (case-insensitive match).
_SECRET_KEY_NAMES: frozenset[str] = frozenset(
    k.lower()
    for k in (
        "api_key",
        "apikey",
        "api-key",
        "authorization",
        "auth",
        "bearer",
        "password",
        "passwd",
        "pwd",
        "secret",
        "secret_key",
        "secret-key",
        "access_token",
        "access-token",
        "refresh_token",
        "refresh-token",
        "credential",
        "credentials",
        "token",
    )
)

# Regex patterns applied to string values to catch inline secrets.
_SECRET_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # OpenAI-style keys
    (re.compile(r"sk-proj-[a-zA-Z0-9_-]{20,}"), _REDACTED),
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"), _REDACTED),
    (re.compile(r"sk-[a-zA-Z0-9]{20,}"), _REDACTED),
    # AWS access keys
    (re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), _REDACTED),
    # GitHub tokens
    (re.compile(r"gh[po]_[a-zA-Z0-9]{36}"), _REDACTED),
    # GitLab tokens
    (re.compile(r"glpat-[a-zA-Z0-9_-]{20,}"), _REDACTED),
    # Slack tokens
    (re.compile(r"xox[bpsa]-[a-zA-Z0-9-]{10,}"), _REDACTED),
    # Google API keys
    (re.compile(r"AIza[a-zA-Z0-9_-]{35}"), _REDACTED),
    # Bearer / Basic auth tokens in strings
    (re.compile(r"(?i)(?:Bearer|Basic)\s+[a-zA-Z0-9._-]{10,}"), _REDACTED),
    # JWT tokens (three base64url segments)
    (re.compile(r"eyJ[a-zA-Z0-9_-]*\.eyJ[a-zA-Z0-9_-]*\.[a-zA-Z0-9_-]*"), _REDACTED),
]


def _redact_string(text: str) -> str:
    """Apply regex-based redaction to a string value."""
    for pattern, replacement in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_secrets(obj: Any, _depth: int = 0) -> Any:
    """Recursively redact secret values from a data structure.

    - Dict values whose *lowered* key is in ``_SECRET_KEY_NAMES`` are replaced
      with ``"[REDACTED]"``.
    - String values are scanned for known API-key / token patterns.
    - Lists and tuples are traversed element-wise.
    - A depth cap (20) prevents stack overflow on deeply nested payloads.
    """
    if _depth > 20:
        return obj

    if isinstance(obj, dict):
        redacted: dict[str, Any] = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEY_NAMES:
                redacted[k] = _REDACTED
            else:
                redacted[k] = _redact_secrets(v, _depth + 1)
        return redacted

    if isinstance(obj, list):
        return [_redact_secrets(item, _depth + 1) for item in obj]

    if isinstance(obj, tuple):
        return tuple(_redact_secrets(item, _depth + 1) for item in obj)

    if isinstance(obj, str):
        return _redact_string(obj)

    # int, float, bool, None, etc. — safe as-is
    return obj


def _timestamp_prefix() -> str:
    """Compact timestamp for filenames: YYYYMMDD_HHMMSSffffff."""
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S%f")


def _serializable(obj: Any) -> Any:
    """Convert an object tree to JSON-serializable primitives."""
    if obj is None:
        return None
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serializable(item) for item in obj]
    # Pydantic / OpenAI SDK objects expose .model_dump()
    if hasattr(obj, "model_dump"):
        return _serializable(obj.model_dump())
    # Dataclasses
    if hasattr(obj, "__dict__"):
        return _serializable(vars(obj))
    return str(obj)


class LLMLogger:
    """Writes one JSON file per LLM request and per LLM response.

    Includes automatic log rotation: old files are pruned by age and count
    every ``cleanup_interval`` writes.
    """

    def __init__(
        self,
        log_dir: str | Path,
        *,
        max_files: int = LLM_LOG_MAX_FILES,
        max_age_days: int = LLM_LOG_MAX_AGE_DAYS,
        cleanup_interval: int = LLM_LOG_CLEANUP_INTERVAL,
    ) -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._max_files = max_files
        self._max_age_days = max_age_days
        self._cleanup_interval = cleanup_interval
        self._write_count = 0

    # ── public helpers ────────────────────────────────────────────────────

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex[:8]

    def _write(self, filename: str, data: Dict[str, Any]) -> Path:
        path = self._dir / filename
        try:
            safe_data = _redact_secrets(data)
            path.write_text(json.dumps(safe_data, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to write LLM log %s: %s", path, exc)
        self._write_count += 1
        if self._write_count % self._cleanup_interval == 0:
            self._cleanup()
        return path

    # ── rotation / cleanup ────────────────────────────────────────────────

    def _cleanup(self) -> None:
        """Remove old files by age, then trim to max_files by count."""
        try:
            files = _list_log_files(self._dir)
        except OSError:
            return

        now = time.time()
        age_cutoff = now - (self._max_age_days * 86400)

        # 1) Delete files older than max_age_days
        remaining: list[Path] = []
        for f in files:
            try:
                if f.stat().st_mtime < age_cutoff:
                    f.unlink()
                else:
                    remaining.append(f)
            except OSError:
                remaining.append(f)

        # 2) Trim to max_files (oldest first — list is already sorted by name)
        if len(remaining) > self._max_files:
            excess = len(remaining) - self._max_files
            for f in remaining[:excess]:
                try:
                    f.unlink()
                except OSError:
                    pass

    def get_log_dir_size(self) -> int:
        """Return total size (bytes) of all log files in the directory."""
        return _dir_size(self._dir)

    # ── request ───────────────────────────────────────────────────────────

    def log_request(
        self,
        request_id: str,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.7,
        max_tokens: Optional[int] = None,
    ) -> datetime:
        """Write the request file. Returns the request timestamp."""
        now = datetime.now(timezone.utc)
        ts_prefix = now.strftime("%Y%m%d_%H%M%S%f")

        payload: Dict[str, Any] = {
            "request_id": request_id,
            "timestamp": now.isoformat(),
            "model": model,
            "messages": _serializable(messages),
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = _serializable(tools)
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        self._write(f"{ts_prefix}_request_{request_id}.json", payload)
        return now

    # ── response ──────────────────────────────────────────────────────────

    def log_response(
        self,
        request_id: str,
        model: str,
        response: Any,
        request_ts: datetime,
        error: Optional[Exception] = None,
    ) -> None:
        """Write the response file, linked by request_id."""
        now = datetime.now(timezone.utc)
        ts_prefix = now.strftime("%Y%m%d_%H%M%S%f")
        duration_ms = (now - request_ts).total_seconds() * 1000

        payload: Dict[str, Any] = {
            "request_id": request_id,
            "timestamp": now.isoformat(),
            "model": model,
            "duration_ms": round(duration_ms, 2),
        }

        if error:
            payload["error"] = str(error)
            payload["error_type"] = type(error).__name__
        else:
            payload["response"] = _serializable(response)

        self._write(f"{ts_prefix}_response_{request_id}.json", payload)


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers (reusable by health checks)
# ─────────────────────────────────────────────────────────────────────────────


def _list_log_files(directory: Path) -> list[Path]:
    """Return sorted list of .json log files in *directory* (oldest first)."""
    return sorted(directory.glob("*.json"))


def _dir_size(directory: Path) -> int:
    """Return total size (bytes) of all files in *directory*."""
    total = 0
    try:
        for f in directory.iterdir():
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total
