"""
rate_limiter.py — Rate limiting for skill execution.

Implements sliding window rate limiting to prevent abuse and resource exhaustion:
  - Per-chat rate limiting (default: 30 calls/minute)
  - Per-skill rate limiting for expensive skills (default: 10 calls/minute)
  - Configurable via environment variables
  - Clear error messages when rate limited

Usage:
    from src.rate_limiter import RateLimiter

    limiter = RateLimiter()
    if not limiter.check_rate_limit(chat_id, skill_name):
        return "Rate limit exceeded. Please wait before trying again."
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, FrozenSet, Optional, Tuple

from src.utils.singleton import get_or_create_singleton, reset_singleton

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Configuration Constants
# ─────────────────────────────────────────────────────────────────────────────

# Default rate limits (calls per minute)
DEFAULT_CHAT_RATE_LIMIT: int = 30
DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT: int = 10

# Window size in seconds (1 minute sliding window)
WINDOW_SIZE_SECONDS: float = 60.0

# Maximum number of chat windows to track (prevents memory growth)
MAX_TRACKED_CHATS: int = 1000

# Maximum timestamps to keep per chat (prevents unbounded memory growth)
MAX_TIMESTAMPS_PER_CHAT: int = 100

# Skills that are considered "expensive" and have stricter limits
EXPENSIVE_SKILLS: FrozenSet[str] = frozenset(
    {
        "web_search",
        "web_fetch",
        "webfetch",
        "http_request",
        "fetch_url",
        "browse",
        "screenshot",
        "playwright",
    }
)


@dataclass
class RateLimitConfig:
    """Configuration for rate limiting."""

    chat_rate_limit: int = DEFAULT_CHAT_RATE_LIMIT
    expensive_skill_rate_limit: int = DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT
    window_size_seconds: float = WINDOW_SIZE_SECONDS
    expensive_skills: FrozenSet[str] = field(default_factory=lambda: EXPENSIVE_SKILLS)

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        """Load configuration from environment variables."""
        chat_limit = os.environ.get("RATE_LIMIT_CHAT_PER_MINUTE", "")
        expensive_limit = os.environ.get("RATE_LIMIT_EXPENSIVE_PER_MINUTES", "")

        return cls(
            chat_rate_limit=int(chat_limit)
            if chat_limit.isdigit()
            else DEFAULT_CHAT_RATE_LIMIT,
            expensive_skill_rate_limit=int(expensive_limit)
            if expensive_limit.isdigit()
            else DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT,
            window_size_seconds=WINDOW_SIZE_SECONDS,
            expensive_skills=EXPENSIVE_SKILLS,
        )


@dataclass
class RateLimitResult:
    """Result of a rate limit check."""

    allowed: bool
    remaining: int
    reset_at: float  # Unix timestamp when the oldest entry in window expires
    retry_after: float  # Seconds to wait before retrying (0 if allowed)
    limit_type: str  # "chat" or "skill"
    limit_value: int  # The actual limit that was hit

    @property
    def message(self) -> str:
        """Return a user-friendly rate limit message."""
        if self.allowed:
            return ""
        wait_seconds = int(self.retry_after) + 1
        if self.limit_type == "skill":
            return (
                f"⚠️ This skill is being used too frequently. "
                f"Please wait {wait_seconds} second{'s' if wait_seconds != 1 else ''} before trying again."
            )
        return (
            f"⚠️ Rate limit exceeded for this conversation. "
            f"Please wait {wait_seconds} second{'s' if wait_seconds != 1 else ''} before sending more requests."
        )


class SlidingWindowTracker:
    """
    Thread-safe sliding window rate limiter tracker.

    Uses a deque to store timestamps, automatically pruning
    entries older than the window size. Deque provides O(1) popleft
    amortized pruning vs O(n) for OrderedDict iteration.
    """

    def __init__(self, window_size_seconds: float, max_limit: int):
        self._window_size = window_size_seconds
        self._max_limit = max_limit
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    def _prune_old_entries(self, now: float) -> None:
        """Remove entries older than the window size (O(1) amortized)."""
        cutoff = now - self._window_size
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def check_and_record(self, now: Optional[float] = None) -> Tuple[bool, int, float]:
        """
        Check if operation is allowed and record the timestamp if so.

        Args:
            now: Current timestamp (uses time.time() if not provided)

        Returns:
            Tuple of (allowed, remaining, retry_after_seconds)
        """
        if now is None:
            now = time.time()

        with self._lock:
            self._prune_old_entries(now)

            current_count = len(self._timestamps)

            if current_count >= self._max_limit:
                # Rate limited - find when the oldest entry expires
                if self._timestamps:
                    oldest_time = self._timestamps[0]
                    retry_after = (oldest_time + self._window_size) - now
                    retry_after = max(0.0, retry_after)
                else:
                    retry_after = 0.0
                return False, 0, retry_after

            # Allowed - record this request
            self._timestamps.append(now)
            remaining = self._max_limit - len(self._timestamps)

            # Calculate reset time (when oldest entry expires)
            if self._timestamps:
                oldest_time = self._timestamps[0]
                reset_at = oldest_time + self._window_size
            else:
                reset_at = now + self._window_size

            return True, remaining, reset_at

    def get_current_count(self, now: Optional[float] = None) -> int:
        """Get the current count of operations in the window."""
        if now is None:
            now = time.time()

        with self._lock:
            self._prune_old_entries(now)
            return len(self._timestamps)


class RateLimiter:
    """
    Rate limiter for skill execution with per-chat and per-skill limits.

    Implements sliding window algorithm for both:
    - Per-chat rate limiting (prevents abuse from single conversation)
    - Per-skill rate limiting (protects expensive external resources)

    Example:
        limiter = RateLimiter()
        result = limiter.check_rate_limit("chat_123", "web_search")
        if not result.allowed:
            return result.message
        # Proceed with skill execution
    """

    def __init__(self, config: Optional[RateLimitConfig] = None):
        """
        Initialize the rate limiter.

        Args:
            config: Rate limit configuration (uses defaults/env if not provided)
        """
        self._config = config or RateLimitConfig.from_env()

        # Per-chat rate limiters (keyed by chat_id)
        self._chat_limiters: Dict[str, SlidingWindowTracker] = {}

        # Per-skill rate limiters for expensive skills (keyed by skill_name)
        self._skill_limiters: Dict[str, SlidingWindowTracker] = {}

        # Lock for managing the limiters dictionaries
        self._limiters_lock = Lock()

    def _get_or_create_chat_limiter(self, chat_id: str) -> SlidingWindowTracker:
        """Get or create a rate limiter for a chat."""
        with self._limiters_lock:
            if chat_id not in self._chat_limiters:
                # Prune old limiters if we have too many
                if len(self._chat_limiters) >= MAX_TRACKED_CHATS:
                    self._prune_inactive_chats()

                self._chat_limiters[chat_id] = SlidingWindowTracker(
                    window_size_seconds=self._config.window_size_seconds,
                    max_limit=self._config.chat_rate_limit,
                )
            return self._chat_limiters[chat_id]

    def _get_or_create_skill_limiter(self, skill_name: str) -> SlidingWindowTracker:
        """Get or create a rate limiter for a skill."""
        with self._limiters_lock:
            if skill_name not in self._skill_limiters:
                self._skill_limiters[skill_name] = SlidingWindowTracker(
                    window_size_seconds=self._config.window_size_seconds,
                    max_limit=self._config.expensive_skill_rate_limit,
                )
            return self._skill_limiters[skill_name]

    def _prune_inactive_chats(self) -> None:
        """Remove inactive chat limiters to prevent memory growth."""
        # Simple approach: remove half of the limiters
        # A more sophisticated approach would track last access time
        if len(self._chat_limiters) > MAX_TRACKED_CHATS // 2:
            keys_to_remove = list(self._chat_limiters.keys())[
                : len(self._chat_limiters) // 2
            ]
            for key in keys_to_remove:
                del self._chat_limiters[key]
            log.debug("Pruned %d inactive chat rate limiters", len(keys_to_remove))

    def is_expensive_skill(self, skill_name: str) -> bool:
        """Check if a skill is considered expensive."""
        return skill_name.lower() in self._config.expensive_skills

    def check_rate_limit(
        self,
        chat_id: str,
        skill_name: str,
        now: Optional[float] = None,
    ) -> RateLimitResult:
        """
        Check if the skill execution is allowed under rate limits.

        Checks both per-chat and per-skill limits. Returns the most
        restrictive result if either limit would be exceeded.

        Args:
            chat_id: The chat/conversation identifier
            skill_name: The name of the skill being executed
            now: Current timestamp (uses time.time() if not provided)

        Returns:
            RateLimitResult with allowed status and details
        """
        if now is None:
            now = time.time()

        # Check per-chat rate limit
        chat_limiter = self._get_or_create_chat_limiter(chat_id)
        chat_allowed, chat_remaining, chat_reset_at = chat_limiter.check_and_record(now)

        # Check per-skill rate limit if this is an expensive skill
        skill_allowed = True
        skill_remaining = 0
        skill_reset_at = now
        is_expensive = self.is_expensive_skill(skill_name)

        if is_expensive:
            skill_limiter = self._get_or_create_skill_limiter(skill_name)
            skill_allowed, skill_remaining, skill_reset_at = (
                skill_limiter.check_and_record(now)
            )

        # Determine the result (most restrictive wins)
        if not chat_allowed:
            retry_after = max(0.0, chat_reset_at - now)
            log.info(
                "Rate limit exceeded for chat %s (chat limit: %d/min)",
                chat_id,
                self._config.chat_rate_limit,
                extra={"chat_id": chat_id, "limit_type": "chat"},
            )
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=chat_reset_at,
                retry_after=retry_after,
                limit_type="chat",
                limit_value=self._config.chat_rate_limit,
            )

        if not skill_allowed:
            retry_after = max(0.0, skill_reset_at - now)
            log.info(
                "Rate limit exceeded for skill %s in chat %s (skill limit: %d/min)",
                skill_name,
                chat_id,
                self._config.expensive_skill_rate_limit,
                extra={"chat_id": chat_id, "skill": skill_name, "limit_type": "skill"},
            )
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=skill_reset_at,
                retry_after=retry_after,
                limit_type="skill",
                limit_value=self._config.expensive_skill_rate_limit,
            )

        # Both checks passed
        log.debug(
            "Rate limit check passed for skill %s in chat %s (chat remaining: %d, skill remaining: %d)",
            skill_name,
            chat_id,
            chat_remaining,
            skill_remaining if is_expensive else "N/A",
        )

        return RateLimitResult(
            allowed=True,
            remaining=min(chat_remaining, skill_remaining)
            if is_expensive
            else chat_remaining,
            reset_at=max(chat_reset_at, skill_reset_at)
            if is_expensive
            else chat_reset_at,
            retry_after=0.0,
            limit_type="skill" if is_expensive else "chat",
            limit_value=self._config.expensive_skill_rate_limit
            if is_expensive
            else self._config.chat_rate_limit,
        )

    def get_chat_usage(self, chat_id: str) -> int:
        """Get the current usage count for a chat in the sliding window."""
        limiter = self._chat_limiters.get(chat_id)
        if limiter:
            return limiter.get_current_count()
        return 0

    def get_skill_usage(self, skill_name: str) -> int:
        """Get the current usage count for a skill in the sliding window."""
        limiter = self._skill_limiters.get(skill_name)
        if limiter:
            return limiter.get_current_count()
        return 0

    def reset_chat(self, chat_id: str) -> None:
        """Reset rate limit tracking for a specific chat."""
        with self._limiters_lock:
            if chat_id in self._chat_limiters:
                del self._chat_limiters[chat_id]
                log.debug("Reset rate limit tracking for chat %s", chat_id)

    def reset_skill(self, skill_name: str) -> None:
        """Reset rate limit tracking for a specific skill."""
        with self._limiters_lock:
            if skill_name in self._skill_limiters:
                del self._skill_limiters[skill_name]
                log.debug("Reset rate limit tracking for skill %s", skill_name)

    def check_message_rate(
        self, chat_id: str, limit: int = 30, window_seconds: int = 60
    ) -> RateLimitResult:
        """
        Check per-chat message rate using sliding window.

        Reuses SlidingWindowTracker for O(1) amortized pruning instead of
        maintaining a separate deque-based implementation.

        Args:
            chat_id: Chat identifier
            limit: Maximum messages allowed in window (default: 30)
            window_seconds: Time window in seconds (default: 60)

        Returns:
            RateLimitResult with allowed status and metadata
        """
        key = f"msg_rate:{chat_id}"

        with self._limiters_lock:
            if key not in self._chat_limiters:
                self._chat_limiters[key] = SlidingWindowTracker(
                    window_size_seconds=float(window_seconds),
                    max_limit=limit,
                )
            tracker = self._chat_limiters[key]

        allowed, remaining_or_zero, reset_at_or_retry = tracker.check_and_record()

        if not allowed:
            return RateLimitResult(
                allowed=False,
                remaining=0,
                reset_at=reset_at_or_retry,
                retry_after=reset_at_or_retry,
                limit_type="message_rate",
                limit_value=limit,
            )

        return RateLimitResult(
            allowed=True,
            remaining=remaining_or_zero,
            reset_at=reset_at_or_retry,
            retry_after=0.0,
            limit_type="message_rate",
            limit_value=limit,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Module-level singleton for convenience (thread-safe)
# ─────────────────────────────────────────────────────────────────────────────

# Module-level instance for backward compatibility
_global_rate_limiter: Optional[RateLimiter] = None


def get_rate_limiter() -> RateLimiter:
    """
    Get the global rate limiter instance (lazy initialization).

    Thread-safe singleton using get_or_create_singleton from utils.
    """
    global _global_rate_limiter
    if _global_rate_limiter is None:
        _global_rate_limiter = get_or_create_singleton(RateLimiter)
    return _global_rate_limiter


def reset_rate_limiter() -> None:
    """Reset the global rate limiter (useful for testing)."""
    global _global_rate_limiter
    _global_rate_limiter = None
    reset_singleton(RateLimiter)


# ─────────────────────────────────────────────────────────────────────────────
# Convenience Exports
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    "RateLimiter",
    "RateLimitConfig",
    "RateLimitResult",
    "SlidingWindowTracker",
    "get_rate_limiter",
    "reset_rate_limiter",
    "EXPENSIVE_SKILLS",
    "DEFAULT_CHAT_RATE_LIMIT",
    "DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT",
]
