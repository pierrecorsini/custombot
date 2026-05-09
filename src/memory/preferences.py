"""
memory.preferences — Auto-detect and store user preferences from conversations.

Tracks communication style, response length, language, and topic interests.
Preferences are only stored after 3+ consistent signals (confidence gating).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MIN_SIGNALS = 3


@dataclass(slots=True)
class PreferenceSignals:
    """Accumulated signals for a single preference dimension."""

    value_counts: dict[str, int] = field(default_factory=dict)

    def record(self, value: str) -> None:
        self.value_counts[value] = self.value_counts.get(value, 0) + 1

    def confident_value(self) -> str | None:
        """Return the value if it has >= MIN_SIGNALS, else None."""
        for value, count in self.value_counts.items():
            if count >= MIN_SIGNALS:
                return value
        return None


def _detect_style(text: str) -> str:
    """Classify message as formal or casual."""
    text_lower = text.lower().strip()
    informal_markers = sum(
        1 for m in ("hey", "hi", "yo", "lol", "haha", "thx", "pls", "u ", "ur ", "np", "ok", "tho")
        if m in text_lower
    )
    sentences = re.split(r"[.!?]+", text)
    avg_len = sum(len(s.split()) for s in sentences if s.strip()) / max(len([s for s in sentences if s.strip()]), 1)
    return "casual" if informal_markers >= 2 or avg_len < 8 else "formal"


def _detect_response_pref(messages: list[dict[str, Any]]) -> str | None:
    """Detect short vs detailed preference from follow-up patterns."""
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if len(user_msgs) < 2:
        return None
    short_followups = 0
    for i in range(1, len(user_msgs)):
        prev = user_msgs[i - 1].get("content", "")
        curr = user_msgs[i].get("content", "")
        if len(prev) > 100 and len(curr) < 30:
            short_followups += 1
    return "short" if short_followups >= 2 else "detailed"


def _detect_language(text: str) -> str:
    """Simple language detection: CJK vs Latin."""
    cjk = len(re.findall(r"[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]", text))
    latin = len(re.findall(r"[a-zA-Z]", text))
    if cjk > latin and cjk > 5:
        if re.search(r"[\u4e00-\u9fff]", text):
            return "zh"
        if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text):
            return "ja"
        return "ko"
    return "en"


class PreferenceLearner:
    """Extracts and persists user preferences per chat."""

    def __init__(self, data_dir: str) -> None:
        self._path = Path(data_dir) / "preferences.json"
        self._preferences: dict[str, dict[str, Any]] = {}
        # Pending signals not yet committed (per chat).
        self._signals: dict[str, dict[str, PreferenceSignals]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._preferences = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load preferences: %s", exc)
            self._preferences = {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._preferences, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def extract_preferences(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Analyze messages and return detected preferences (not yet stored)."""
        if not messages:
            return {}

        user_msgs = [m for m in messages if m.get("role") == "user"]
        if not user_msgs:
            return {}

        all_text = " ".join(m.get("content", "") for m in user_msgs)

        detected: dict[str, Any] = {}
        detected["communication_style"] = _detect_style(all_text)
        detected["language"] = _detect_language(all_text)

        response_pref = _detect_response_pref(messages)
        if response_pref:
            detected["response_length"] = response_pref

        # Topic interests: most common significant words
        words = re.findall(r"[a-zA-Z]{4,}", all_text.lower())
        stop_words = frozenset({
            "that", "this", "with", "from", "have", "they", "been", "were",
            "will", "would", "could", "about", "which", "their", "there",
            "what", "when", "where", "your", "just", "like", "know",
        })
        filtered = [w for w in words if w not in stop_words]
        if filtered:
            freq: dict[str, int] = {}
            for w in filtered:
                freq[w] = freq.get(w, 0) + 1
            top_topics = sorted(freq, key=freq.get, reverse=True)[:5]  # type: ignore[arg-type]
            detected["topic_interests"] = top_topics

        return detected

    def get_preferences(self, chat_id: str) -> dict[str, Any]:
        """Return stored preferences for a chat."""
        return self._preferences.get(chat_id, {})

    def update_preference(self, chat_id: str, key: str, value: Any) -> None:
        """Directly set a preference value (bypasses confidence gating)."""
        self._preferences.setdefault(chat_id, {})[key] = value
        self._save()

    def process_messages(self, chat_id: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
        """Extract preferences from messages, apply confidence gating, store.

        Returns the current stored preferences for the chat.
        """
        detected = self.extract_preferences(messages)
        if not detected:
            return self.get_preferences(chat_id)

        chat_signals = self._signals.setdefault(chat_id, {})
        for key, value in detected.items():
            if key == "topic_interests":
                # Topic interests bypass gating — always updated
                self._preferences.setdefault(chat_id, {})[key] = value
                continue
            signal_key = f"{key}"
            if signal_key not in chat_signals:
                chat_signals[signal_key] = PreferenceSignals()
            chat_signals[signal_key].record(str(value))

            confident = chat_signals[signal_key].confident_value()
            if confident is not None:
                self._preferences.setdefault(chat_id, {})[key] = value

        self._save()
        return self.get_preferences(chat_id)
