"""
stealth.py — Anti-detection helpers for WhatsApp bot.

Human-like timing with log-normal distributions (natural bell curve),
per-chat cooldown tracking, and typing pause patterns.
"""

from __future__ import annotations

import random
import time
from collections import OrderedDict

# ─────────────────────────────────────────────────────────────────────────────
# Per-chat cooldown tracker (OrderedDict for O(1) LRU eviction)
# ─────────────────────────────────────────────────────────────────────────────

_MIN_COOLDOWN = 3.0  # seconds between replies to the same chat
_MAX_TRACKED_CHATS = 1000  # LRU cap for cooldown tracking
_last_sent: OrderedDict[str, float] = OrderedDict()


def cooldown_remaining(chat_id: str) -> float:
    """Return seconds remaining before this chat can receive another reply."""
    last = _last_sent.get(chat_id, 0.0)
    elapsed = time.monotonic() - last
    return max(0.0, _MIN_COOLDOWN - elapsed)


def mark_sent(chat_id: str) -> None:
    """Record that a reply was just sent to this chat (O(1) LRU eviction)."""
    if chat_id not in _last_sent and len(_last_sent) >= _MAX_TRACKED_CHATS:
        _last_sent.popitem(last=False)  # O(1) eviction of oldest
    _last_sent[chat_id] = time.monotonic()
    _last_sent.move_to_end(chat_id)  # Mark as most recently used


# ─────────────────────────────────────────────────────────────────────────────
# Human-like delays
# ─────────────────────────────────────────────────────────────────────────────


def _lognorm(mu: float, sigma: float, lo: float, hi: float) -> float:
    """Log-normal random value clamped to [lo, hi]."""
    return min(max(random.lognormvariate(mu, sigma), lo), hi)


def read_delay(message_len: int) -> float:
    """Simulate time to 'read' an incoming message before thinking.

    Longer messages take more time to read.
    """
    if message_len < 50:
        return _lognorm(mu=0.3, sigma=0.4, lo=0.3, hi=2.0)
    if message_len < 200:
        return _lognorm(mu=0.7, sigma=0.4, lo=0.8, hi=3.5)
    return _lognorm(mu=1.0, sigma=0.4, lo=1.5, hi=5.0)


def think_delay() -> float:
    """Simulate 'thinking' pause before starting to type."""
    return _lognorm(mu=0.6, sigma=0.5, lo=0.5, hi=4.0)


def type_delay(response_len: int) -> float:
    """Simulate typing duration proportional to response length.

    Average typing speed ~200 chars/sec on desktop → we use slower
    ~50-70 chars/sec to be realistic for mobile-linked responses.
    """
    chars_per_sec = random.uniform(50.0, 80.0)
    base = response_len / chars_per_sec
    jitter = _lognorm(mu=0.2, sigma=0.3, lo=0.1, hi=1.5)
    return min(base + jitter, 8.0)


def typing_pause_duration() -> float:
    """Occasional mid-typing pause (human behavior: look away, re-read).

    Only triggered ~30% of the time; returns 0 otherwise.
    """
    if random.random() > 0.3:
        return 0.0
    return _lognorm(mu=0.5, sigma=0.4, lo=0.3, hi=2.0)


def full_send_delay(incoming_len: int, response_len: int) -> float:
    """Combined read + think + type delay for one reply."""
    return read_delay(incoming_len) + think_delay() + type_delay(response_len)
