"""Tests for src/utils/path.py — sanitize_path_component caching."""

from __future__ import annotations

import pytest

from src.utils.path import sanitize_path_component


class TestSanitizePathComponentCaching:
    """Verify @functools.lru_cache on sanitize_path_component()."""

    def setup_method(self) -> None:
        sanitize_path_component.cache_clear()

    def teardown_method(self) -> None:
        sanitize_path_component.cache_clear()

    def test_repeated_call_returns_cached_object(self) -> None:
        """Two calls with the same input return the exact same string object."""
        first = sanitize_path_component("user@s.whatsapp.net")
        second = sanitize_path_component("user@s.whatsapp.net")
        assert first is second

    def test_cache_info_records_hits(self) -> None:
        """Subsequent calls with the same key register as cache hits."""
        sanitize_path_component("chat-1")
        sanitize_path_component("chat-1")
        info = sanitize_path_component.cache_info()
        assert info.hits >= 1
        assert info.misses == 1

    def test_different_inputs_record_separate_misses(self) -> None:
        """Distinct inputs each cause a cache miss."""
        sanitize_path_component("alice")
        sanitize_path_component("bob")
        info = sanitize_path_component.cache_info()
        assert info.misses == 2
        assert info.hits == 0

    def test_cache_clear_resets(self) -> None:
        """After cache_clear(), the next call is a miss again."""
        sanitize_path_component("chat-x")
        sanitize_path_component("chat-x")
        assert sanitize_path_component.cache_info().hits >= 1

        sanitize_path_component.cache_clear()
        sanitize_path_component("chat-x")
        info = sanitize_path_component.cache_info()
        assert info.hits == 0
        assert info.misses == 1

    def test_value_error_not_cached(self) -> None:
        """Exceptions are not cached — every call with empty string raises."""
        for _ in range(3):
            with pytest.raises(ValueError):
                sanitize_path_component("")
        # Misses should reflect the failed attempts (lru_cache does not cache
        # exceptions, so each is a fresh call).
        info = sanitize_path_component.cache_info()
        assert info.hits == 0

    def test_special_character_inputs_cached_independently(self) -> None:
        """Different chat IDs that produce different sanitized outputs."""
        r1 = sanitize_path_component("a:b")
        r2 = sanitize_path_component("a/b")
        assert r1 != r2
        # Both should be cached independently
        sanitize_path_component("a:b")
        sanitize_path_component("a/b")
        info = sanitize_path_component.cache_info()
        assert info.hits == 2
