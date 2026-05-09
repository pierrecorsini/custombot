"""
event_store.py — Event-sourced message store for audit trails and replay.

Stores events as immutable facts (JSONL append-only) enabling:
  - Full audit trail of every message, tool call, and response
  - Replay debugging by reconstructing chat state from events
  - Zero-cost when disabled (no file I/O)

Enable via config: ``event_store_enabled = true``
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from enum import StrEnum
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = [
    "EventStore",
    "EventType",
    "StoredEvent",
    "ChatState",
]


class EventType(StrEnum):
    """Known event types in the event store."""

    MESSAGE_RECEIVED = "message_received"
    TOOL_CALLED = "tool_called"
    RESPONSE_GENERATED = "response_generated"
    RESPONSE_DELIVERED = "response_delivered"


@dataclass(slots=True, frozen=True)
class StoredEvent:
    """Immutable event record stored as one JSONL line."""

    event_id: str
    event_type: EventType
    timestamp: float
    chat_id: str
    payload: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(asdict(self), default=str)

    @classmethod
    def from_json(cls, line: str) -> StoredEvent:
        data = json.loads(line)
        data["event_type"] = EventType(data["event_type"])
        return cls(**data)


@dataclass
class ChatState:
    """Reconstructed chat state from replayed events."""

    chat_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    responses: list[str] = field(default_factory=list)

    def apply(self, event: StoredEvent) -> None:
        """Apply a single event to mutate state."""
        if event.event_type == EventType.MESSAGE_RECEIVED:
            self.messages.append(event.payload)
        elif event.event_type == EventType.TOOL_CALLED:
            self.tool_calls.append(event.payload)
        elif event.event_type == EventType.RESPONSE_GENERATED:
            self.responses.append(event.payload.get("content", ""))
        elif event.event_type == EventType.RESPONSE_DELIVERED:
            pass  # delivery confirmation — no state change


class EventStore:
    """Append-only JSONL event store for audit trails and replay debugging.

    Thread-safe for single asyncio event-loop usage.  Each event is
    written as one JSON line so files are human-readable and recoverable.
    """

    __slots__ = ("_dir", "_enabled")

    def __init__(self, data_dir: str, *, enabled: bool = True) -> None:
        self._dir = Path(data_dir) / "events"
        self._enabled = enabled
        if enabled:
            self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _event_file(self, chat_id: str) -> Path:
        safe_id = chat_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe_id}.jsonl"

    def append(self, event: StoredEvent) -> None:
        """Append an event to the chat's JSONL file."""
        if not self._enabled:
            return
        path = self._event_file(event.chat_id)
        line = event.to_json() + "\n"
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)

    def create_event(
        self,
        event_type: EventType,
        chat_id: str,
        payload: dict[str, Any],
        timestamp: float,
    ) -> StoredEvent:
        """Create a new StoredEvent with a generated UUID."""
        return StoredEvent(
            event_id=uuid.uuid4().hex,
            event_type=event_type,
            timestamp=timestamp,
            chat_id=chat_id,
            payload=payload,
        )

    def get_events(
        self,
        chat_id: str,
        from_timestamp: float = 0.0,
    ) -> list[StoredEvent]:
        """Read events for a chat, optionally filtering by timestamp."""
        path = self._event_file(chat_id)
        if not path.exists():
            return []

        events: list[StoredEvent] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = StoredEvent.from_json(line)
                except (json.JSONDecodeError, ValueError):
                    log.warning("Skipping malformed event line: %.80s", line)
                    continue
                if event.timestamp >= from_timestamp:
                    events.append(event)
        return events

    def rebuild_chat_state(self, chat_id: str) -> ChatState:
        """Replay all events to reconstruct the current chat state."""
        events = self.get_events(chat_id)
        state = ChatState(chat_id=chat_id)
        for event in events:
            state.apply(event)
        return state
