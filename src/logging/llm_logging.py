"""
src/logging/llm_logging.py — Per-file LLM request/response logging.

When enabled, each LLM call produces two JSON files in workspace/logs/llm/:

  {timestamp}_request_{request_id}.json   — full request payload
  {timestamp}_response_{request_id}.json  — full response payload

Both files share the same request_id so they can be paired.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)


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
    """Writes one JSON file per LLM request and per LLM response."""

    def __init__(self, log_dir: str | Path) -> None:
        self._dir = Path(log_dir)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── public helpers ────────────────────────────────────────────────────

    @staticmethod
    def new_request_id() -> str:
        return uuid.uuid4().hex[:8]

    def _write(self, filename: str, data: Dict[str, Any]) -> Path:
        path = self._dir / filename
        try:
            path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            log.warning("Failed to write LLM log %s: %s", path, exc)
        return path

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
