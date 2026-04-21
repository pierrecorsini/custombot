"""
test_skill_name_validation.py — Unit tests for skill name validation.

Verifies that SkillRegistry.register() rejects names containing
characters outside [a-z0-9_].
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.skills import SkillRegistry
from src.skills.base import BaseSkill


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
            "",                          # empty
            "WebSearch",                 # uppercase
            "web-search",                # hyphen
            "web.search",                # dot
            "skill; DROP TABLE--",       # SQL injection attempt
            "hello world",               # space
            "naïve",                     # non-ASCII
            "skill/name",                # slash
            'quote"inname',              # double quote
            "back`tick",                 # backtick
            "new\nline",                 # newline
            "tab\there",                 # tab
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
