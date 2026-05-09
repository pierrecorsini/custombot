"""
src/monitoring/quality_metrics.py — Conversation quality metrics tracking.

Tracks conversational quality indicators:
- Average turns per conversation session
- Tool call success rate
- User follow-up rate (messages within 60s of bot response)

A session is defined as a sequence of messages where consecutive
messages arrive within 5 minutes of each other.

Usage:
    from src.monitoring.quality_metrics import ConversationQualityMetrics

    qm = ConversationQualityMetrics()
    qm.record_user_message("chat_1", timestamp)
    qm.record_bot_response("chat_1", timestamp)
    qm.record_tool_call("chat_1", success=True)
    stats = qm.get_stats()
"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from dataclasses import dataclass

# Session timeout: messages within this window belong to the same session.
SESSION_TIMEOUT_SECONDS: float = 300.0  # 5 minutes

# Follow-up window: user messages within this time after bot response
# are considered follow-ups (indicating unsatisfying first response).
FOLLOWUP_WINDOW_SECONDS: float = 60.0

# Maximum number of chats tracked for quality metrics.
MAX_TRACKED_CHATS: int = 200

# Bounded deques for rolling rate calculations.
HISTORY_SIZE: int = 100


@dataclass(slots=True)
class QualityStats:
    """Snapshot of conversation quality metrics."""

    avg_turns_per_session: float = 0.0
    total_sessions: int = 0
    tool_call_success_rate: float = 0.0
    tool_calls_total: int = 0
    tool_calls_succeeded: int = 0
    user_followup_rate: float = 0.0
    total_bot_responses: int = 0
    total_followups: int = 0

    def to_dict(self) -> dict[str, float | int]:
        return {
            "avg_turns_per_session": round(self.avg_turns_per_session, 2),
            "total_sessions": self.total_sessions,
            "tool_call_success_rate": round(self.tool_call_success_rate, 4),
            "tool_calls_total": self.tool_calls_total,
            "tool_calls_succeeded": self.tool_calls_succeeded,
            "user_followup_rate": round(self.user_followup_rate, 4),
            "total_bot_responses": self.total_bot_responses,
            "total_followups": self.total_followups,
        }


@dataclass(slots=True)
class _ChatSession:
    """Tracks turns within a single conversation session for one chat."""

    last_message_time: float = 0.0
    turn_count: int = 0


class ConversationQualityMetrics:
    """Track conversation quality indicators with bounded memory.

    Thread-safety: relies on asyncio's single-threaded event loop.
    """

    def __init__(
        self,
        session_timeout: float = SESSION_TIMEOUT_SECONDS,
        followup_window: float = FOLLOWUP_WINDOW_SECONDS,
    ) -> None:
        self._session_timeout = session_timeout
        self._followup_window = followup_window

        # Per-chat session tracking (bounded LRU)
        self._chat_sessions: OrderedDict[str, _ChatSession] = OrderedDict()

        # Completed session turn counts (rolling window)
        self._completed_session_turns: deque[int] = deque(maxlen=HISTORY_SIZE)

        # Tool call tracking
        self._tool_calls_total: int = 0
        self._tool_calls_succeeded: int = 0

        # Follow-up tracking
        self._last_bot_response_time: dict[str, float] = {}
        self._total_bot_responses: int = 0
        self._total_followups: int = 0

    def record_user_message(self, chat_id: str, timestamp: float | None = None) -> None:
        """Record a user message and detect follow-ups / session boundaries."""
        ts = timestamp or time.time()

        # Check for follow-up (user message within window of last bot response)
        last_bot_ts = self._last_bot_response_time.get(chat_id)
        if last_bot_ts is not None and (ts - last_bot_ts) < self._followup_window:
            self._total_followups += 1

        # Update session tracking
        session = self._chat_sessions.get(chat_id)
        if session is not None:
            if (ts - session.last_message_time) < self._session_timeout:
                # Same session — increment turn count
                session.turn_count += 1
                session.last_message_time = ts
                self._chat_sessions.move_to_end(chat_id)
                return
            else:
                # Session timed out — finalize previous session
                self._completed_session_turns.append(session.turn_count)

        # New session
        self._ensure_capacity()
        self._chat_sessions[chat_id] = _ChatSession(
            last_message_time=ts, turn_count=1,
        )
        self._chat_sessions.move_to_end(chat_id)

    def record_bot_response(self, chat_id: str, timestamp: float | None = None) -> None:
        """Record a bot response for follow-up detection."""
        ts = timestamp or time.time()
        self._last_bot_response_time[chat_id] = ts
        self._total_bot_responses += 1

        # Count as a turn in the current session
        session = self._chat_sessions.get(chat_id)
        if session is not None:
            session.turn_count += 1
            session.last_message_time = ts
            self._chat_sessions.move_to_end(chat_id)

    def record_tool_call(self, chat_id: str, success: bool) -> None:
        """Record a tool call outcome for success rate tracking."""
        self._tool_calls_total += 1
        if success:
            self._tool_calls_succeeded += 1

    def get_stats(self) -> QualityStats:
        """Return a snapshot of conversation quality metrics."""
        # Finalize active sessions for avg calculation
        active_turns = [s.turn_count for s in self._chat_sessions.values()]
        all_turns = list(self._completed_session_turns) + active_turns

        total_sessions = len(all_turns)
        avg_turns = sum(all_turns) / total_sessions if total_sessions else 0.0

        tool_rate = (
            self._tool_calls_succeeded / self._tool_calls_total
            if self._tool_calls_total
            else 0.0
        )

        followup_rate = (
            self._total_followups / self._total_bot_responses
            if self._total_bot_responses
            else 0.0
        )

        return QualityStats(
            avg_turns_per_session=avg_turns,
            total_sessions=total_sessions,
            tool_call_success_rate=tool_rate,
            tool_calls_total=self._tool_calls_total,
            tool_calls_succeeded=self._tool_calls_succeeded,
            user_followup_rate=followup_rate,
            total_bot_responses=self._total_bot_responses,
            total_followups=self._total_followups,
        )

    def _ensure_capacity(self) -> None:
        """Evict oldest half when at capacity (LRU eviction)."""
        if len(self._chat_sessions) < MAX_TRACKED_CHATS:
            return
        for _ in range(len(self._chat_sessions) // 2):
            evicted_key, session = self._chat_sessions.popitem(last=False)
            self._completed_session_turns.append(session.turn_count)
