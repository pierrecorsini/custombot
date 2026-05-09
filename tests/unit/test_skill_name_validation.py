"""
test_skill_name_validation.py — Unit tests for skill name validation
and tool-definitions caching.

Verifies that SkillRegistry.register() rejects names containing
characters outside [a-z0-9_], and that tool_definitions is cached
and invalidated correctly.
"""

from __future__ import annotations


import pytest

from src.skills import SkillRegistry
from src.skills.base import BaseSkill
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def _make_skill(name: str) -> BaseSkill:
    """Create a minimal skill with the given name."""

    class _TestSkill(BaseSkill):
        async def execute(self, workspace_dir: Path, **kwargs) -> str:
            return "ok"

    _TestSkill.name = name  # type: ignore[attr-defined]
    return _TestSkill()


class TestValidSkillNames:
    """Names that SHOULD be accepted."""

    @pytest.mark.parametrize(
        "name",
        [
            "web_search",
            "readfile",
            "a",
            "skill123",
            "_leading_underscore",
            "trailing_underscore_",
            "multiple__underscores",
            "123_numbers_first",
        ],
    )
    def test_accepts_valid_name(self, name: str):
        registry = SkillRegistry()
        skill = _make_skill(name)
        registry.register(skill)
        assert registry.get(name) is skill

    def test_allows_re_registration_of_valid_name(self):
        registry = SkillRegistry()
        registry.register(_make_skill("my_skill"))
        replacement = _make_skill("my_skill")
        registry.register(replacement)
        assert registry.get("my_skill") is replacement


class TestInvalidSkillNames:
    """Names that SHOULD be rejected."""

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "WebSearch",  # uppercase
            "web-search",  # hyphen
            "web.search",  # dot
            "skill; DROP TABLE--",  # SQL injection attempt
            "hello world",  # space
            "naïve",  # non-ASCII
            "skill/name",  # slash
            'quote"inname',  # double quote
            "back`tick",  # backtick
            "new\nline",  # newline
            "tab\there",  # tab
        ],
    )
    def test_rejects_invalid_name(self, name: str):
        registry = SkillRegistry()
        registry.register(_make_skill(name))
        assert registry.get(name) is None

    def test_rejects_empty_name(self):
        registry = SkillRegistry()
        registry.register(_make_skill(""))
        assert len(registry.all()) == 0


class TestToolDefinitionsCaching:
    """Verify tool_definitions is cached, not rebuilt on every access."""

    def test_100_accesses_trigger_single_rebuild(self):
        """100 consecutive property accesses should compute once, not 100 times."""
        registry = SkillRegistry()
        registry.register(_make_skill("alpha"))
        registry.register(_make_skill("beta"))

        first = registry.tool_definitions
        assert len(first) == 2

        for _ in range(99):
            assert registry.tool_definitions is first

    def test_cache_invalidated_on_register(self):
        """Registering a new skill must invalidate the cache."""
        registry = SkillRegistry()
        registry.register(_make_skill("alpha"))

        first = registry.tool_definitions
        assert len(first) == 1

        registry.register(_make_skill("beta"))
        second = registry.tool_definitions

        assert second is not first
        assert len(second) == 2

class TestReRegistrationInvalidatesCache:
    """Replacing an existing skill must invalidate the cache."""

    def test_re_registration_invalidates_cache(self):
        registry = SkillRegistry()
        registry.register(_make_skill("alpha"))
        first = registry.tool_definitions

        registry.register(_make_skill("alpha"))
        second = registry.tool_definitions

        assert second is not first


class TestSkillRegistryCacheStats:
    """Verify SkillRegistry tracks lookup hit/miss counters."""

    def test_hit_on_existing_skill(self):
        registry = SkillRegistry()
        skill = _make_skill("web_search")
        registry.register(skill)

        result = registry.get("web_search")
        assert result is skill
        assert registry.cache_stats == {"hits": 1, "misses": 0}

    def test_miss_on_unknown_skill(self):
        registry = SkillRegistry()

        result = registry.get("nonexistent")
        assert result is None
        assert registry.cache_stats == {"hits": 0, "misses": 1}

    def test_counters_accumulate_across_calls(self):
        registry = SkillRegistry()
        registry.register(_make_skill("alpha"))
        registry.register(_make_skill("beta"))

        registry.get("alpha")   # hit
        registry.get("beta")    # hit
        registry.get("gamma")   # miss
        registry.get("alpha")   # hit

        assert registry.cache_stats == {"hits": 3, "misses": 1}

    def test_replaced_skill_still_counts_as_hit(self):
        registry = SkillRegistry()
        registry.register(_make_skill("alpha"))

        registry.get("alpha")  # hit

        replacement = _make_skill("alpha")
        registry.register(replacement)
        registry.get("alpha")  # hit (replacement found)

        assert registry.cache_stats == {"hits": 2, "misses": 0}
        assert registry.get("alpha") is replacement

    def test_cache_stats_returns_new_dict_each_call(self):
        """cache_stats should return a fresh dict, not a mutable reference."""
        registry = SkillRegistry()
        registry.register(_make_skill("alpha"))
        registry.get("alpha")

        stats1 = registry.cache_stats
        stats2 = registry.cache_stats
        assert stats1 is not stats2
        assert stats1 == stats2
