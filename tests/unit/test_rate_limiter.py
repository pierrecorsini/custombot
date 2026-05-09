"""
Tests for src/rate_limiter.py — SlidingWindowTracker and RateLimiter.

Covers:
- check_only() + record() two-phase API
- check_only() does NOT record timestamp
- Sliding window expiry (old entries pruned)
- RateLimiter per-chat limits
- RateLimiter expensive skill detection
- check_message_rate() uses separate tracker
- Hypothesis property-based tests for SlidingWindowTracker edge cases
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from src.rate_limiter import (
    EXPENSIVE_SKILLS,
    RateLimitConfig,
    RateLimiter,
    RateLimitResult,
    SlidingWindowTracker,
)


# ─────────────────────────────────────────────────────────────────────────────
# Test SlidingWindowTracker
# ─────────────────────────────────────────────────────────────────────────────


class TestSlidingWindowCheckAndRecord:
    """Tests for SlidingWindowTracker check_only() + record() two-phase API."""

    def test_allows_within_limit(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=5)
        allowed, remaining, reset_at = tracker.check_only(now=100.0)
        assert allowed is True
        assert remaining == 5  # No timestamps recorded yet

        tracker.record(now=100.0)
        assert tracker.get_current_count(now=100.0) == 1

    def test_records_timestamp(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=5)
        allowed, _, _ = tracker.check_only(now=100.0)
        assert allowed is True

        tracker.record(now=100.0)
        assert tracker.get_current_count(now=100.0) == 1

    def test_denies_over_limit(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=3)

        # Fill up to the limit
        for i in range(3):
            tracker.check_only(now=100.0 + i)
            tracker.record(now=100.0 + i)

        # 4th should be denied
        allowed, remaining, retry_after = tracker.check_only(now=100.0 + 3)

        assert allowed is False
        assert remaining == 0
        assert retry_after > 0

    def test_returns_correct_remaining(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=5)

        tracker.check_only(now=100.0)
        tracker.record(now=100.0)
        _, remaining_2, _ = tracker.check_only(now=100.0)
        assert remaining_2 == 4  # 5 - 1 recorded

        tracker.record(now=100.0)
        _, remaining_3, _ = tracker.check_only(now=100.0)
        assert remaining_3 == 3  # 5 - 2 recorded


class TestSlidingWindowCheckOnly:
    """Tests for SlidingWindowTracker.check_only()."""

    def test_does_not_record_timestamp(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=5)

        tracker.check_only(now=100.0)

        # Should NOT have recorded anything
        assert tracker.get_current_count(now=100.0) == 0

    def test_returns_allowed_when_under_limit(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=5)

        allowed, remaining, reset_at = tracker.check_only(now=100.0)

        assert allowed is True
        assert remaining == 5  # Full capacity still available

    def test_returns_denied_when_at_limit(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=2)
        tracker.check_only(now=100.0)
        tracker.record(now=100.0)
        tracker.check_only(now=100.1)
        tracker.record(now=100.1)

        allowed, _, retry_after = tracker.check_only(now=100.2)

        assert allowed is False

    def test_check_only_then_record_manually(self) -> None:
        """Two-phase: check_only passes, then record() commits."""
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=5)

        allowed, _, _ = tracker.check_only(now=100.0)
        assert allowed is True

        tracker.record(now=100.0)
        assert tracker.get_current_count(now=100.0) == 1


class TestSlidingWindowExpiry:
    """Tests for sliding window pruning of old entries."""

    def test_old_entries_pruned(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=10.0, max_limit=5)

        # Record at t=100
        tracker.check_only(now=100.0)
        tracker.record(now=100.0)
        assert tracker.get_current_count(now=100.0) == 1

        # At t=111, the entry at t=100 is outside the 10s window
        count = tracker.get_current_count(now=111.0)
        assert count == 0

    def test_window_slides_correctly(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=3)

        # Record 3 at t=100
        for t in [100.0, 101.0, 102.0]:
            tracker.check_only(now=t)
            tracker.record(now=t)

        # At t=103, still at limit
        allowed, _, _ = tracker.check_only(now=103.0)
        assert allowed is False

        # At t=161, the entry at t=100 has expired (100 + 60 = 160)
        # So there's room for one more
        allowed, _, _ = tracker.check_only(now=161.0)
        assert allowed is True

    def test_get_current_count_prunes(self) -> None:
        tracker = SlidingWindowTracker(window_size_seconds=5.0, max_limit=10)
        tracker.check_only(now=100.0)
        tracker.record(now=100.0)
        tracker.check_only(now=101.0)
        tracker.record(now=101.0)

        # At t=106, entry at t=100 has expired
        assert tracker.get_current_count(now=106.0) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Test RateLimiter
# ─────────────────────────────────────────────────────────────────────────────


class TestRateLimiterPerChat:
    """Tests for RateLimiter per-chat rate limiting."""

    def test_allows_within_chat_limit(self) -> None:
        config = RateLimitConfig(chat_rate_limit=30, expensive_skill_rate_limit=10)
        limiter = RateLimiter(config=config)

        result = limiter.check_rate_limit("chat_1", "read_file")

        assert result.allowed is True
        assert result.limit_type == "chat"

    def test_denies_over_chat_limit(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=3,
            expensive_skill_rate_limit=10,
            expensive_skills=frozenset(),  # no expensive skills
        )
        limiter = RateLimiter(config=config)

        # Use up the chat limit
        now = time.time()
        for _ in range(3):
            limiter.check_rate_limit("chat_1", "basic_skill", now=now)

        # 4th call should be denied
        result = limiter.check_rate_limit("chat_1", "basic_skill", now=now)
        assert result.allowed is False
        assert result.limit_type == "chat"

    def test_different_chats_independent(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=2,
            expensive_skill_rate_limit=10,
            expensive_skills=frozenset(),
        )
        limiter = RateLimiter(config=config)

        now = time.time()
        # Fill chat_1
        limiter.check_rate_limit("chat_1", "s", now=now)
        limiter.check_rate_limit("chat_1", "s", now=now)

        # chat_2 should still be allowed
        result = limiter.check_rate_limit("chat_2", "s", now=now)
        assert result.allowed is True


class TestRateLimiterExpensiveSkill:
    """Tests for expensive skill detection and rate limiting."""

    def test_is_expensive_skill_true(self) -> None:
        limiter = RateLimiter()
        assert limiter.is_expensive_skill("web_search") is True
        assert limiter.is_expensive_skill("Web_Search") is True  # case-insensitive

    def test_is_expensive_skill_false(self) -> None:
        limiter = RateLimiter()
        assert limiter.is_expensive_skill("read_file") is False

    def test_expensive_skill_has_stricter_limit(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=30,
            expensive_skill_rate_limit=2,
        )
        limiter = RateLimiter(config=config)

        now = time.time()
        # Use up the expensive skill limit
        limiter.check_rate_limit("chat_1", "web_search", now=now)
        limiter.check_rate_limit("chat_1", "web_search", now=now)

        # 3rd call to expensive skill should be denied
        result = limiter.check_rate_limit("chat_1", "web_search", now=now)
        assert result.allowed is False
        assert result.limit_type == "skill"

    def test_expensive_skill_message_mentions_skill(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=30,
            expensive_skill_rate_limit=1,
        )
        limiter = RateLimiter(config=config)

        now = time.time()
        limiter.check_rate_limit("chat_1", "web_search", now=now)

        result = limiter.check_rate_limit("chat_1", "web_search", now=now)
        assert result.allowed is False
        assert "skill" in result.message.lower()


class TestRateLimiterCheckMessageRate:
    """Tests for RateLimiter.check_message_rate() — separate tracker."""

    def test_uses_separate_tracker(self) -> None:
        """check_message_rate does not consume skill/chat rate slots."""
        config = RateLimitConfig(
            chat_rate_limit=1,
            expensive_skill_rate_limit=10,
            expensive_skills=frozenset(),
        )
        limiter = RateLimiter(config=config)

        # Use up the chat rate limit
        now = time.time()
        limiter.check_rate_limit("chat_1", "skill_a", now=now)

        # Chat rate should be exhausted for skills
        skill_result = limiter.check_rate_limit("chat_1", "skill_b", now=now)
        assert skill_result.allowed is False

        # But message rate should be independent
        msg_result = limiter.check_message_rate("chat_1", limit=30)
        assert msg_result.allowed is True
        assert msg_result.limit_type == "message_rate"

    def test_message_rate_tracks_separately(self) -> None:
        config = RateLimitConfig()
        limiter = RateLimiter(config=config)

        # Record many message rate checks
        for _ in range(30):
            limiter.check_message_rate("chat_1", limit=30)

        # 31st should be denied
        result = limiter.check_message_rate("chat_1", limit=30)
        assert result.allowed is False

    def test_message_rate_result_has_correct_type(self) -> None:
        limiter = RateLimiter()
        result = limiter.check_message_rate("chat_1")

        assert isinstance(result, RateLimitResult)
        assert result.limit_type == "message_rate"

    def test_different_chats_independent_for_messages(self) -> None:
        limiter = RateLimiter()

        # Exhaust messages for chat_1
        for _ in range(30):
            limiter.check_message_rate("chat_1", limit=30)

        # chat_2 should still be allowed
        result = limiter.check_message_rate("chat_2", limit=30)
        assert result.allowed is True


class TestMessageRateSlotConsumption:
    """Tests verifying check_message_rate() consumes slots on allowed messages.

    The two-phase check_only() + record() pattern must:
    - Decrement remaining on each allowed call
    - NOT consume a slot when denied (so retry_after stays stable)
    - Reflect consumed slots in subsequent check_only() results

    Note: check_message_rate() returns `remaining` from check_only() which
    is computed BEFORE record() commits the slot.  So after i recorded calls
    the returned remaining = limit - i  (not limit - i - 1).
    """

    @pytest.mark.parametrize("limit", [1, 3, 5, 10, 30])
    def test_allowed_messages_consume_slots(self, limit: int) -> None:
        """Each allowed message decrements the remaining count seen by the next call."""
        limiter = RateLimiter()
        fake_time = 1000.0

        with patch("time.time", return_value=fake_time):
            for i in range(limit):
                result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)
                assert result.allowed is True, f"Call {i + 1}/{limit} should be allowed"
                # remaining comes from check_only() BEFORE record(), so it
                # reports limit - i  (the count of timestamps already present).
                assert result.remaining == limit - i, (
                    f"On call {i + 1}, remaining should be {limit - i}"
                )

            # The call that exhausts the limit must be denied
            result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)
            assert result.allowed is False

    @pytest.mark.parametrize("limit", [2, 5, 10])
    def test_denied_does_not_consume_slot(self, limit: int) -> None:
        """Repeated calls after exhaustion do not add more timestamps."""
        limiter = RateLimiter()
        fake_time = 1000.0

        with patch("time.time", return_value=fake_time):
            # Exhaust all slots
            for _ in range(limit):
                limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)

            # Two denied calls at the same instant
            denied_1 = limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)
            assert denied_1.allowed is False

            denied_2 = limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)
            assert denied_2.allowed is False

            # retry_after must be identical — no new timestamps were recorded,
            # so the oldest timestamp (and thus the reset time) is unchanged.
            assert denied_1.retry_after == denied_2.retry_after

    @pytest.mark.parametrize("limit", [3, 5])
    def test_slot_exhaustion_precisely_at_limit(self, limit: int) -> None:
        """Exactly `limit` allowed calls followed by a denied call."""
        limiter = RateLimiter()
        fake_time = 1000.0

        with patch("time.time", return_value=fake_time):
            for i in range(limit):
                result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)
                assert result.allowed is True

            # Next call must be denied — no slot was leaked
            result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=60)
            assert result.allowed is False
            assert result.remaining == 0

    @pytest.mark.parametrize("limit,window", [(3, 10), (5, 60)])
    def test_slots_recover_after_window_expiry(self, limit: int, window: int) -> None:
        """Slots become available again after the sliding window expires."""
        limiter = RateLimiter()

        base_time = 1000.0
        with patch("time.time", return_value=base_time):
            for i in range(limit):
                result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=window)
                assert result.allowed is True

            # Exhausted at the base time
            result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=window)
            assert result.allowed is False

        # After window expires, all old timestamps are pruned → slots recover
        with patch("time.time", return_value=base_time + window + 1):
            result = limiter.check_message_rate("chat_1", limit=limit, window_seconds=window)
        assert result.allowed is True
        # check_only sees 0 existing timestamps, remaining = limit - 0 = limit
        # then record() adds 1 → effective remaining = limit - 1
        assert result.remaining == limit

    def test_partial_slot_recovery(self) -> None:
        """Only expired entries are pruned; recent ones still count."""
        limiter = RateLimiter()
        window = 60

        # Record 3 messages at different times
        with patch("time.time", return_value=100.0):
            limiter.check_message_rate("chat_1", limit=3, window_seconds=window)
        with patch("time.time", return_value=130.0):
            limiter.check_message_rate("chat_1", limit=3, window_seconds=window)
        with patch("time.time", return_value=150.0):
            limiter.check_message_rate("chat_1", limit=3, window_seconds=window)

        # At t=161, the entry at t=100 has expired (100 + 60 = 160)
        # but entries at t=130 and t=150 are still within the window
        with patch("time.time", return_value=161.0):
            result = limiter.check_message_rate("chat_1", limit=3, window_seconds=window)
        assert result.allowed is True
        # check_only sees 2 timestamps (t=130, t=150), remaining = 3 - 2 = 1
        # then record() adds one more for 3 total
        assert result.remaining == 1


class TestRateLimiterReset:
    """Tests for reset_chat() and reset_skill()."""

    def test_reset_chat_clears_tracking(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=1,
            expensive_skill_rate_limit=10,
            expensive_skills=frozenset(),
        )
        limiter = RateLimiter(config=config)

        now = time.time()
        limiter.check_rate_limit("chat_1", "s", now=now)

        # Exhausted
        assert limiter.check_rate_limit("chat_1", "s", now=now).allowed is False

        # Reset
        limiter.reset_chat("chat_1")

        # Should be allowed again
        assert limiter.check_rate_limit("chat_1", "s", now=now).allowed is True

    def test_reset_skill_clears_tracking(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=30,
            expensive_skill_rate_limit=1,
        )
        limiter = RateLimiter(config=config)

        now = time.time()
        limiter.check_rate_limit("chat_1", "web_search", now=now)

        # Exhausted
        result = limiter.check_rate_limit("chat_1", "web_search", now=now)
        assert result.allowed is False

        # Reset
        limiter.reset_skill("web_search")

        # Should be allowed again
        assert limiter.check_rate_limit("chat_1", "web_search", now=now).allowed is True


class TestRateLimitResultMessage:
    """Tests for RateLimitResult.message property."""

    def test_allowed_returns_empty_string(self) -> None:
        result = RateLimitResult(
            allowed=True,
            remaining=5,
            reset_at=0.0,
            retry_after=0.0,
            limit_type="chat",
            limit_value=30,
        )
        assert result.message == ""

    def test_chat_limit_message(self) -> None:
        result = RateLimitResult(
            allowed=False,
            remaining=0,
            reset_at=0.0,
            retry_after=15.0,
            limit_type="chat",
            limit_value=30,
        )
        msg = result.message
        assert "Rate limit exceeded" in msg
        assert "16 second" in msg  # int(15.0) + 1 = 16

    def test_skill_limit_message(self) -> None:
        result = RateLimitResult(
            allowed=False,
            remaining=0,
            reset_at=0.0,
            retry_after=5.0,
            limit_type="skill",
            limit_value=10,
        )
        msg = result.message
        assert "skill" in msg.lower()
        assert "frequently" in msg


class TestRateLimitConfigFromEnvBounds:
    """Tests for RateLimitConfig.from_env() env-var bounds validation."""

    def test_defaults_when_no_env_vars(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            config = RateLimitConfig.from_env()
        from src.constants import DEFAULT_CHAT_RATE_LIMIT, DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT

        assert config.chat_rate_limit == DEFAULT_CHAT_RATE_LIMIT
        assert config.expensive_skill_rate_limit == DEFAULT_EXPENSIVE_SKILL_RATE_LIMIT

    def test_valid_env_vars_accepted(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMIT_CHAT_PER_MINUTE": "50"}, clear=False):
            config = RateLimitConfig.from_env()
        assert config.chat_rate_limit == 50

    def test_too_high_chat_limit_clamped_to_max(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMIT_CHAT_PER_MINUTE": "999999"}, clear=False):
            config = RateLimitConfig.from_env()
        from src.constants import RATE_LIMIT_MAX_VALUE

        assert config.chat_rate_limit == RATE_LIMIT_MAX_VALUE

    def test_too_high_expensive_limit_clamped_to_max(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMIT_EXPENSIVE_PER_MINUTES": "999999"}, clear=False):
            config = RateLimitConfig.from_env()
        from src.constants import RATE_LIMIT_MAX_VALUE

        assert config.expensive_skill_rate_limit == RATE_LIMIT_MAX_VALUE

    def test_zero_chat_limit_clamped_to_min(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMIT_CHAT_PER_MINUTE": "0"}, clear=False):
            config = RateLimitConfig.from_env()
        from src.constants import RATE_LIMIT_MIN_VALUE

        assert config.chat_rate_limit == RATE_LIMIT_MIN_VALUE

    def test_zero_expensive_limit_clamped_to_min(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMIT_EXPENSIVE_PER_MINUTES": "0"}, clear=False):
            config = RateLimitConfig.from_env()
        from src.constants import RATE_LIMIT_MIN_VALUE

        assert config.expensive_skill_rate_limit == RATE_LIMIT_MIN_VALUE

    def test_negative_treated_as_non_digit_uses_default(self) -> None:
        """Negative values have a '-' prefix, so .isdigit() returns False."""
        with patch.dict("os.environ", {"RATE_LIMIT_CHAT_PER_MINUTE": "-5"}, clear=False):
            config = RateLimitConfig.from_env()
        from src.constants import DEFAULT_CHAT_RATE_LIMIT

        assert config.chat_rate_limit == DEFAULT_CHAT_RATE_LIMIT

    def test_non_numeric_treated_as_default(self) -> None:
        with patch.dict("os.environ", {"RATE_LIMIT_CHAT_PER_MINUTE": "abc"}, clear=False):
            config = RateLimitConfig.from_env()
        from src.constants import DEFAULT_CHAT_RATE_LIMIT

        assert config.chat_rate_limit == DEFAULT_CHAT_RATE_LIMIT

    def test_boundary_max_accepted(self) -> None:
        from src.constants import RATE_LIMIT_MAX_VALUE

        with patch.dict(
            "os.environ",
            {"RATE_LIMIT_CHAT_PER_MINUTE": str(RATE_LIMIT_MAX_VALUE)},
            clear=False,
        ):
            config = RateLimitConfig.from_env()
        assert config.chat_rate_limit == RATE_LIMIT_MAX_VALUE

    def test_boundary_min_accepted(self) -> None:
        from src.constants import RATE_LIMIT_MIN_VALUE

        with patch.dict(
            "os.environ",
            {"RATE_LIMIT_CHAT_PER_MINUTE": str(RATE_LIMIT_MIN_VALUE)},
            clear=False,
        ):
            config = RateLimitConfig.from_env()
        assert config.chat_rate_limit == RATE_LIMIT_MIN_VALUE

    def test_logs_effective_values(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.INFO, logger="src.rate_limiter"):
            with patch.dict(
                "os.environ",
                {"RATE_LIMIT_CHAT_PER_MINUTE": "25", "RATE_LIMIT_EXPENSIVE_PER_MINUTES": "5"},
                clear=False,
            ):
                RateLimitConfig.from_env()

        assert "chat_limit=25/min" in caplog.text
        assert "expensive_skill_limit=5/min" in caplog.text

    def test_logs_warning_on_clamping(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        with caplog.at_level(logging.WARNING, logger="src.rate_limiter"):
            with patch.dict("os.environ", {"RATE_LIMIT_CHAT_PER_MINUTE": "999999"}, clear=False):
                RateLimitConfig.from_env()

        assert "out of bounds" in caplog.text
        assert "clamping" in caplog.text

    def test_logs_warning_when_chat_exceeds_effective_max(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Values within hard bounds but above EFFECTIVE_MAX trigger an advisory warning."""
        import logging
        from src.constants import RATE_LIMIT_EFFECTIVE_MAX

        with caplog.at_level(logging.WARNING, logger="src.rate_limiter"):
            with patch.dict(
                "os.environ",
                {"RATE_LIMIT_CHAT_PER_MINUTE": str(RATE_LIMIT_EFFECTIVE_MAX + 1)},
                clear=False,
            ):
                RateLimitConfig.from_env()

        assert "exceeds advisory effective max" in caplog.text
        assert "nearly disabled" in caplog.text

    def test_logs_warning_when_expensive_exceeds_effective_max(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Expensive skill limit above EFFECTIVE_MAX triggers advisory warning."""
        import logging
        from src.constants import RATE_LIMIT_EFFECTIVE_MAX

        with caplog.at_level(logging.WARNING, logger="src.rate_limiter"):
            with patch.dict(
                "os.environ",
                {"RATE_LIMIT_EXPENSIVE_PER_MINUTES": str(RATE_LIMIT_EFFECTIVE_MAX + 1)},
                clear=False,
            ):
                RateLimitConfig.from_env()

        assert "RATE_LIMIT_EXPENSIVE_PER_MINUTES" in caplog.text
        assert "exceeds advisory effective max" in caplog.text

    def test_no_effective_max_warning_within_advisory(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Values at or below EFFECTIVE_MAX produce no advisory warning."""
        import logging
        from src.constants import RATE_LIMIT_EFFECTIVE_MAX

        with caplog.at_level(logging.WARNING, logger="src.rate_limiter"):
            with patch.dict(
                "os.environ",
                {
                    "RATE_LIMIT_CHAT_PER_MINUTE": str(RATE_LIMIT_EFFECTIVE_MAX),
                    "RATE_LIMIT_EXPENSIVE_PER_MINUTES": str(RATE_LIMIT_EFFECTIVE_MAX),
                },
                clear=False,
            ):
                RateLimitConfig.from_env()

        assert "exceeds advisory effective max" not in caplog.text

    def test_effective_max_warning_not_emitted_for_clamped_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Out-of-bounds values get clamped first; advisory only fires if the
        clamped result still exceeds EFFECTIVE_MAX."""
        import logging

        with caplog.at_level(logging.WARNING, logger="src.rate_limiter"):
            with patch.dict(
                "os.environ",
                {"RATE_LIMIT_CHAT_PER_MINUTE": "5"},  # well within EFFECTIVE_MAX
                clear=False,
            ):
                RateLimitConfig.from_env()

        assert "exceeds advisory effective max" not in caplog.text


class TestRegisterExpensiveSkill:
    """Tests for RateLimiter.register_expensive_skill() — dynamic expensive set."""

    def test_adds_new_skill_as_expensive(self) -> None:
        config = RateLimitConfig(expensive_skills=frozenset())
        limiter = RateLimiter(config=config)

        limiter.register_expensive_skill("my_custom_skill")

        assert limiter.is_expensive_skill("my_custom_skill") is True

    def test_case_insensitive(self) -> None:
        config = RateLimitConfig(expensive_skills=frozenset())
        limiter = RateLimiter(config=config)

        limiter.register_expensive_skill("MySkill")

        assert limiter.is_expensive_skill("myskill") is True
        assert limiter.is_expensive_skill("MYSKILL") is True

    def test_idempotent(self) -> None:
        config = RateLimitConfig(expensive_skills=frozenset())
        limiter = RateLimiter(config=config)

        limiter.register_expensive_skill("x")
        limiter.register_expensive_skill("x")

        assert limiter.is_expensive_skill("x") is True

    def test_preserves_existing_expensive_skills(self) -> None:
        limiter = RateLimiter()

        limiter.register_expensive_skill("my_new_skill")

        assert limiter.is_expensive_skill("web_search") is True
        assert limiter.is_expensive_skill("my_new_skill") is True

    def test_dynamically_registered_skill_gets_stricter_limit(self) -> None:
        config = RateLimitConfig(
            chat_rate_limit=30,
            expensive_skill_rate_limit=1,
            expensive_skills=frozenset(),
        )
        limiter = RateLimiter(config=config)
        limiter.register_expensive_skill("custom_api")

        now = time.time()
        limiter.check_rate_limit("chat_1", "custom_api", now=now)

        result = limiter.check_rate_limit("chat_1", "custom_api", now=now)
        assert result.allowed is False
        assert result.limit_type == "skill"

    def test_logs_registration(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging

        config = RateLimitConfig(expensive_skills=frozenset())
        limiter = RateLimiter(config=config)

        with caplog.at_level(logging.INFO, logger="src.rate_limiter"):
            limiter.register_expensive_skill("new_skill")

        assert "Registered expensive skill: new_skill" in caplog.text


# ─────────────────────────────────────────────────────────────────────────────
# Hypothesis property-based tests for SlidingWindowTracker
# ─────────────────────────────────────────────────────────────────────────────

_FINITE_FLOAT = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
_POSITIVE_FLOAT = st.floats(min_value=0.001, max_value=1e6, allow_nan=False, allow_infinity=False)


class TestSlidingWindowHypothesis:
    """Property-based tests for SlidingWindowTracker edge cases.

    Covers adversarial timestamp sequences (identical, decreasing),
    boundary window sizes (near-zero, very large), prune correctness,
    and rate calculation invariants under extreme conditions.
    """

    # --- Prune correctness with monotonic timestamps ---

    @given(
        timestamps=st.lists(_FINITE_FLOAT, min_size=1, max_size=100).map(sorted),
        window=_POSITIVE_FLOAT,
        max_limit=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=300)
    def test_prune_count_matches_window_invariant(
        self, timestamps: list[float], window: float, max_limit: int
    ) -> None:
        """For monotonic timestamps, count equals entries where ts >= now - window."""
        tracker = SlidingWindowTracker(window_size_seconds=window, max_limit=max_limit)
        for ts in timestamps:
            tracker.record(now=ts)

        now = timestamps[-1]
        count = tracker.get_current_count(now=now)
        expected = sum(1 for ts in timestamps if ts >= now - window)
        assert count == expected

    # --- Identical timestamps ---

    @given(
        now=_FINITE_FLOAT,
        n_records=st.integers(min_value=1, max_value=100),
    )
    @settings(max_examples=200)
    def test_identical_timestamps_all_counted(self, now: float, n_records: int) -> None:
        """Recording N events at the same timestamp results in count == N."""
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=1000)
        for _ in range(n_records):
            tracker.record(now=now)

        assert tracker.get_current_count(now=now) == n_records

    # --- check_only read-only invariant ---

    @given(
        timestamps=st.lists(_FINITE_FLOAT, min_size=1, max_size=50).map(sorted),
        window=_POSITIVE_FLOAT,
        max_limit=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=200)
    def test_check_only_never_modifies_count(
        self, timestamps: list[float], window: float, max_limit: int
    ) -> None:
        """Repeated check_only() calls never change internal state."""
        tracker = SlidingWindowTracker(window_size_seconds=window, max_limit=max_limit)
        for ts in timestamps:
            tracker.record(now=ts)

        now = timestamps[-1]
        count_before = tracker.get_current_count(now=now)

        for _ in range(20):
            tracker.check_only(now=now)

        assert tracker.get_current_count(now=now) == count_before

    # --- Retry-after non-negative when denied ---

    @given(
        fill_count=st.integers(min_value=1, max_value=20),
        max_limit=st.integers(min_value=1, max_value=10),
        window=st.floats(min_value=0.1, max_value=100.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=300)
    def test_retry_after_nonnegative_when_denied(
        self, fill_count: int, max_limit: int, window: float
    ) -> None:
        """retry_after is always >= 0 when the request is denied."""
        assume(fill_count >= max_limit)

        tracker = SlidingWindowTracker(window_size_seconds=window, max_limit=max_limit)
        now = 1000.0
        for i in range(fill_count):
            tracker.record(now=now + i * 0.001)

        allowed, _, retry_after = tracker.check_only(now=now + fill_count * 0.001)
        assert allowed is False
        assert retry_after >= 0.0

    # --- Remaining bounded when allowed ---

    @given(
        n_records=st.integers(min_value=0, max_value=49),
        max_limit=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=200)
    def test_remaining_within_bounds_when_allowed(
        self, n_records: int, max_limit: int
    ) -> None:
        """When allowed, remaining == max_limit - n_in_window."""
        assume(n_records < max_limit)

        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=max_limit)
        now = 1000.0
        for _ in range(n_records):
            tracker.record(now=now)

        allowed, remaining, _ = tracker.check_only(now=now)
        assert allowed is True
        assert 0 <= remaining <= max_limit
        assert remaining == max_limit - n_records

    # --- Near-zero window expires quickly ---

    @given(
        record_time=st.floats(
            min_value=100.0, max_value=1e6, allow_nan=False, allow_infinity=False
        ),
        window=st.floats(min_value=0.001, max_value=1.0, allow_nan=False, allow_infinity=False),
        overshoot=st.floats(min_value=0.1, max_value=10.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_small_window_expires_after_window_plus_overshoot(
        self, record_time: float, window: float, overshoot: float
    ) -> None:
        """With a small window, entries expire after window + overshoot seconds."""
        tracker = SlidingWindowTracker(window_size_seconds=window, max_limit=10)
        tracker.record(now=record_time)

        assert tracker.get_current_count(now=record_time) == 1
        assert tracker.get_current_count(now=record_time + window + overshoot) == 0

    # --- Very large window keeps everything ---

    @given(
        timestamps=st.lists(_FINITE_FLOAT, min_size=1, max_size=50),
        max_limit=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=200)
    def test_very_large_window_preserves_all(
        self, timestamps: list[float], max_limit: int
    ) -> None:
        """With a huge window (1e9s ≈ 31 years), all recorded entries are retained."""
        tracker = SlidingWindowTracker(window_size_seconds=1e9, max_limit=max_limit)
        for ts in timestamps:
            tracker.record(now=ts)

        now = max(timestamps) + 1.0
        assert tracker.get_current_count(now=now) == len(timestamps)

    # --- Backward timestamps don't resurrect pruned entries ---

    @given(
        early_time=st.floats(
            min_value=100.0, max_value=1000.0, allow_nan=False, allow_infinity=False
        ),
        window=st.floats(min_value=1.0, max_value=60.0, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_pruned_entries_never_resurrected(self, early_time: float, window: float) -> None:
        """Going back in time doesn't resurrect already-pruned entries."""
        tracker = SlidingWindowTracker(window_size_seconds=window, max_limit=10)
        tracker.record(now=early_time)

        # Advance past the window so the entry is pruned
        late_time = early_time + window + 100.0
        assert tracker.get_current_count(now=late_time) == 0

        # Going back to the original time must NOT resurrect the entry
        assert tracker.get_current_count(now=early_time) == 0

    # --- max_limit = 0 always denies ---

    @given(now=_FINITE_FLOAT)
    @settings(max_examples=100)
    def test_max_limit_zero_always_denied(self, now: float) -> None:
        """With max_limit=0, check_only always denies with retry_after=0."""
        tracker = SlidingWindowTracker(window_size_seconds=60.0, max_limit=0)
        allowed, remaining, retry_after = tracker.check_only(now=now)
        assert allowed is False
        assert remaining == 0
        assert retry_after == 0.0

    # --- Exact cutoff boundary ---

    @given(
        window=st.floats(min_value=0.1, max_value=1000.0, allow_nan=False, allow_infinity=False),
        now=st.floats(min_value=100.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    )
    @settings(max_examples=200)
    def test_entries_at_exact_cutoff_kept(self, window: float, now: float) -> None:
        """Entries at exactly (now - window) are kept; just before cutoff are pruned."""
        cutoff_time = now - window

        tracker_kept = SlidingWindowTracker(window_size_seconds=window, max_limit=10)
        tracker_kept.record(now=cutoff_time)
        assert tracker_kept.get_current_count(now=now) == 1

        tracker_pruned = SlidingWindowTracker(window_size_seconds=window, max_limit=10)
        just_before = cutoff_time - 0.0001
        tracker_pruned.record(now=just_before)
        assert tracker_pruned.get_current_count(now=now) == 0


class TestSlidingWindowTrackerProperties:
    """Property-based tests for SlidingWindowTracker consistency.

    Verifies three core invariants:
    1. Rate limit is never exceeded when request count is within limit
    2. After window expiry, count resets correctly and slots recover
    3. Consistent (deterministic) results for the same inputs
    """

    # --- Invariant 1: Within-limit requests always allowed ---

    @given(
        limit=st.integers(min_value=1, max_value=100),
        window=st.integers(min_value=60, max_value=300),
    )
    @settings(max_examples=100)
    def test_within_limit_always_allowed(self, limit: int, window: int) -> None:
        """Every request up to and including the limit must be allowed."""
        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        for i in range(limit):
            allowed, remaining, _ = tracker.check_only(now=base_time + i * 0.1)
            assert allowed is True, f"Request {i + 1}/{limit} should be allowed"
            tracker.record(now=base_time + i * 0.1)

    @given(
        limit=st.integers(min_value=1, max_value=100),
        window=st.integers(min_value=60, max_value=300),
    )
    @settings(max_examples=100)
    def test_exceeding_limit_rejected(self, limit: int, window: int) -> None:
        """The first request beyond the limit is always rejected."""
        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        for i in range(limit):
            tracker.record(now=base_time + i * 0.1)

        allowed, remaining, retry_after = tracker.check_only(now=base_time + limit * 0.1)
        assert allowed is False, "Request over limit should be rejected"
        assert remaining == 0
        assert retry_after > 0

    @given(
        limit=st.integers(min_value=2, max_value=50),
        window=st.integers(min_value=60, max_value=120),
    )
    @settings(max_examples=100)
    def test_remaining_decreases_with_each_record(
        self, limit: int, window: int
    ) -> None:
        """Each record() call decreases remaining by exactly 1."""
        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        for i in range(limit):
            allowed, remaining, _ = tracker.check_only(now=base_time)
            assert allowed is True
            assert remaining == limit - i
            tracker.record(now=base_time)

    # --- Invariant 2: Window expiry resets count ---

    @given(
        limit=st.integers(min_value=1, max_value=50),
        window=st.integers(min_value=10, max_value=120),
        extra_requests=st.integers(min_value=1, max_value=50),
    )
    @settings(max_examples=100)
    def test_count_resets_after_window_expiry(
        self, limit: int, window: int, extra_requests: int
    ) -> None:
        """After window expires, count drops to 0 and new requests are allowed."""
        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        # Fill up to the limit
        for i in range(limit):
            tracker.record(now=base_time + i * 0.1)

        count_before = tracker.get_current_count(now=base_time + limit * 0.1)
        assert count_before == limit

        # After window expiry + buffer, all old timestamps are pruned.
        # Use a generous gap to avoid boundary issues with the last timestamp.
        expired_time = base_time + float(window) + float(window)
        count_after = tracker.get_current_count(now=expired_time)
        assert count_after == 0

        # New requests within limit should be allowed again
        for j in range(min(extra_requests, limit)):
            allowed, _, _ = tracker.check_only(now=expired_time + j * 0.1)
            assert allowed is True, f"Request {j + 1} after window expiry should be allowed"
            tracker.record(now=expired_time + j * 0.1)

    @given(
        limit=st.integers(min_value=1, max_value=30),
        window=st.integers(min_value=2, max_value=120),
    )
    @settings(max_examples=100)
    def test_partial_recovery_only_expired_entries_pruned(
        self, limit: int, window: int
    ) -> None:
        """Only timestamps older than the window are pruned; recent ones persist."""
        assume(limit >= 3)
        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        # Record one entry near the start of the window
        tracker.record(now=base_time)
        # Record another midway through
        tracker.record(now=base_time + float(window) / 2.0)
        # Record one near the end
        tracker.record(now=base_time + float(window) - 0.1)

        # Advance just past the window for the first entry only
        check_time = base_time + float(window) + 0.05
        count = tracker.get_current_count(now=check_time)
        # First entry expired, middle and end still within
        assert count == 2

    # --- Invariant 3: Consistent results for same inputs ---

    @given(
        limit=st.integers(min_value=1, max_value=50),
        window=st.integers(min_value=1, max_value=120),
    )
    @settings(max_examples=100)
    def test_consistent_results_for_same_inputs(
        self, limit: int, window: int
    ) -> None:
        """check_only() with identical inputs always returns the same result."""
        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        # Record a few entries
        for i in range(min(limit, 5)):
            tracker.record(now=base_time + i)

        now = base_time + 10.0
        results = [tracker.check_only(now=now) for _ in range(10)]

        # Every call must return the same tuple
        assert all(r == results[0] for r in results), (
            "Same inputs must produce identical results"
        )

    @given(
        limit=st.integers(min_value=1, max_value=50),
        window=st.integers(min_value=1, max_value=120),
        n_records=st.integers(min_value=0, max_value=49),
    )
    @settings(max_examples=100)
    def test_allowed_status_consistent_with_count(
        self, limit: int, window: int, n_records: int
    ) -> None:
        """allowed == (count < limit), no matter how we got there."""
        assume(n_records < limit)

        tracker = SlidingWindowTracker(window_size_seconds=float(window), max_limit=limit)
        base_time = 1000.0

        for _ in range(n_records):
            tracker.record(now=base_time)

        count = tracker.get_current_count(now=base_time)
        allowed, remaining, _ = tracker.check_only(now=base_time)

        assert count == n_records
        assert allowed is True
        assert remaining == limit - count
