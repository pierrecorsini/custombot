"""
src/skills/lazy_loader.py — Lazy skill loading with import-on-first-use.

Defers importing and instantiating skill modules until they are first
requested, reducing startup latency for deployments with many skills.
Core skills (always-needed) are eagerly loaded; optional skills are
registered as module paths and materialized on demand.

Usage::

    from src.skills.lazy_loader import LazySkillLoader

    loader = LazySkillLoader(registry=skill_registry)
    loader.register_lazy("web_research", "src.skills.builtin.web_research:WebResearchSkill")
    # On first call to get(), the module is imported and the skill instantiated.
    skill = loader.get("web_research")
"""

from __future__ import annotations

import importlib
import logging
import time
from typing import TYPE_CHECKING

from src.skills.base import BaseSkill

if TYPE_CHECKING:
    from src.skills import SkillRegistry

log = logging.getLogger(__name__)


class LazySkillLoader:
    """Lazy-load skill modules on first access.

    Stores module paths (``"module.path:ClassName"``) instead of eagerly
    importing them.  When a skill is first requested via :meth:`get`, the
    module is imported and the class instantiated.

    Tracks per-skill lazy-load latency for diagnostics.
    """

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry
        # module_path -> skill_name
        self._pending: dict[str, str] = {}
        # skill_name -> (latency_seconds, was_lazy)
        self._load_metrics: dict[str, tuple[float, bool]] = {}

    def register_lazy(self, name: str, module_path: str) -> None:
        """Register a skill for lazy loading.

        Args:
            name: Skill name (used as the tool function name).
            module_path: Dotted import path with class name,
                e.g. ``"src.skills.builtin.web_research:WebResearchSkill"``.
        """
        self._pending[module_path] = name
        log.debug("Lazy-registered skill %r → %s", name, module_path)

    def register_eager(self, skill: BaseSkill) -> None:
        """Register an already-instantiated skill (core skills, always loaded)."""
        self._registry.register(skill)
        self._load_metrics[skill.name] = (0.0, False)

    def get(self, name: str) -> BaseSkill | None:
        """Get a skill, lazy-loading it if not yet materialized.

        Returns ``None`` if the skill is not registered (neither lazy nor eager).
        """
        # Check if already loaded in registry
        skill = self._registry.get(name)
        if skill is not None:
            return skill

        # Check if pending lazy load
        for module_path, skill_name in list(self._pending.items()):
            if skill_name == name:
                loaded = self._load_skill(module_path, skill_name)
                if loaded is not None:
                    self._pending.pop(module_path, None)
                return loaded

        return None

    def load_all_pending(self) -> int:
        """Force-load all pending lazy skills. Returns count loaded."""
        loaded_count = 0
        for module_path, skill_name in list(self._pending.items()):
            if self._load_skill(module_path, skill_name) is not None:
                loaded_count += 1
        self._pending.clear()
        return loaded_count

    @property
    def pending_count(self) -> int:
        """Number of skills still pending lazy load."""
        return len(self._pending)

    @property
    def metrics(self) -> dict[str, tuple[float, bool]]:
        """Per-skill load metrics: ``{name: (latency_seconds, was_lazy)}``."""
        return dict(self._load_metrics)

    def _load_skill(self, module_path: str, name: str) -> BaseSkill | None:
        """Import and instantiate a skill from its module path."""
        start = time.monotonic()
        try:
            if ":" in module_path:
                module_name, class_name = module_path.rsplit(":", 1)
            else:
                module_name = module_path
                class_name = name

            module = importlib.import_module(module_name)
            skill_class = getattr(module, class_name)
            skill = skill_class()
            self._registry.register(skill)

            latency = time.monotonic() - start
            self._load_metrics[name] = (latency, True)
            log.debug(
                "Lazy-loaded skill %r in %.3fs from %s",
                name,
                latency,
                module_path,
            )
            return skill
        except Exception as exc:
            latency = time.monotonic() - start
            self._load_metrics[name] = (latency, True)
            log.error(
                "Failed to lazy-load skill %r from %s: %s",
                name,
                module_path,
                exc,
                exc_info=True,
            )
            return None
