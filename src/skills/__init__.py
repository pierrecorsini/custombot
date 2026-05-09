"""
src/skills/__init__.py — Skill registry and dynamic loader.

Loads skills from two sources:
  1. Built-in Python skills  (src/skills/builtin/)
  2. User-authored skills    (workspace/skills/)
       • Python files that define a class inheriting BaseSkill
       • Markdown SKILL.md files (prompt skills, skills.sh-style)

Supports per-skill version tracking, dependency declarations, and
hot-loading of skills at runtime without restart.

Usage:
    registry = SkillRegistry()
    registry.load_builtins(db)              # pass Database for routing skills
    registry.load_user_skills("workspace/skills")
    tools = registry.tool_definitions       # cached property
    skill = registry.get("web_search")
    result = await skill.execute(workspace_dir, query="hello")
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import re
import warnings
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from src.skills.base import BaseSkill
from src.skills.sandbox import SandboxConfig, SandboxViolation, SkillSandbox
from src.core.errors import NonCriticalCategory, log_noncritical

if TYPE_CHECKING:
    from src.config.config_schema_defs import ShellConfig
    from src.core.instruction_loader import InstructionLoader
    from src.core.project_context import ProjectContextLoader
    from src.db import Database
    from src.llm import LLMProvider
    from src.project.store import ProjectStore
    from src.routing import RoutingEngine
    from src.vector_memory import VectorMemory

log = logging.getLogger(__name__)

# Valid skill names: lowercase alphanumeric and underscores only.
_VALID_SKILL_NAME = re.compile(r"^[a-z0-9_]+$")

# Modules that user skills should NOT import during loading.
# This is a best-effort restriction — it won't stop determined code
# but catches accidental misuse of dangerous stdlib modules.
_RESTRICTED_MODULES = frozenset(
    {
        "ctypes",
        "multiprocessing",
        "signal",
        "socket",
        "subprocess",
        "sys",
    }
)


@dataclass(slots=True, frozen=True)
class SkillMeta:
    """Metadata tracked per registered skill."""

    version: str = "1.0.0"
    dependencies: tuple[str, ...] = ()
    dangerous: bool = False
    expensive: bool = False


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: Dict[str, BaseSkill] = {}
        self._meta: Dict[str, SkillMeta] = {}

        # Cache effectiveness counters for skill lookups via get().
        self._lookup_hits: int = 0
        self._lookup_misses: int = 0

    # ── registration ───────────────────────────────────────────────────────

    def register_skill(
        self,
        skill: BaseSkill,
        version: str = "1.0.0",
        dependencies: Sequence[str] = (),
    ) -> None:
        """Register a skill with version and dependency metadata."""
        self.register(skill)
        if skill.name in self._skills:
            self._meta[skill.name] = SkillMeta(
                version=version,
                dependencies=tuple(dependencies),
                dangerous=getattr(skill, "dangerous", False),
                expensive=getattr(skill, "expensive", False),
            )

    def register(self, skill: BaseSkill) -> None:
        if not skill.name:
            log.warning("Skill %r has no name, skipping.", type(skill).__name__)
            return
        if not _VALID_SKILL_NAME.match(skill.name):
            log.warning(
                "Skill name %r is invalid (must match [a-z0-9_]+), skipping.",
                skill.name,
            )
            return
        self._skills[skill.name] = skill
        # Invalidate cached tool definitions when skills change
        self.__dict__.pop("tool_definitions", None)
        # Ensure metadata entry exists (defaults if not set via register_skill)
        if skill.name not in self._meta:
            self._meta[skill.name] = SkillMeta(
                dangerous=getattr(skill, "dangerous", False),
                expensive=getattr(skill, "expensive", False),
            )
        log.debug("Registered skill: %s", skill.name)

    def wire_llm_clients(self, llm: "LLMProvider") -> None:
        """Inject the LLM client into all skills that declare a need."""
        wired = 0
        for skill in self._skills.values():
            if skill.needs_llm():
                try:
                    skill.wire_llm(llm)
                except Exception:
                    log_noncritical(
                        NonCriticalCategory.SKILL_PARSING,
                        "Failed to wire LLM client into skill %r",
                        logger=log,
                        extra={"skill": skill.name},
                    )
                    continue
                wired += 1
        if wired:
            log.debug("Wired LLM client into %d skill(s)", wired)

    # ── version / dependency queries ────────────────────────────────────────

    def get_version(self, skill_name: str) -> str:
        """Return the registered version of a skill, or '0.0.0' if unknown."""
        meta = self._meta.get(skill_name)
        return meta.version if meta else "0.0.0"

    @property
    def version_map(self) -> Dict[str, str]:
        """Snapshot of all skill names → versions."""
        return {name: meta.version for name, meta in self._meta.items()}

    def check_dependencies(self) -> list[str]:
        """Return names of skills whose declared dependencies are unmet."""
        available = set(self._skills)
        missing: list[str] = []
        for name, meta in self._meta.items():
            for dep in meta.dependencies:
                if dep not in available:
                    missing.append(f"{name} requires {dep}")
        return missing

    def hot_load(self, module_path: str) -> list[str]:
        """Load skills from a Python module at runtime without restart.

        Args:
            module_path: Filesystem path to the .py module.

        Returns:
            List of skill names that were successfully loaded.
        """
        path = Path(module_path)
        if not path.is_file() or path.suffix != ".py":
            log.warning("hot_load: %s is not a .py file", module_path)
            return []

        loaded: list[str] = []
        # Reuse the existing _load_python_skill logic, but also
        # check manifests when available.
        self._load_python_skill(path)

        # Collect names that were just registered (present in _skills
        # and not previously tracked in _meta).
        for name, skill in list(self._skills.items()):
            if name not in self._meta:
                self._meta[name] = SkillMeta(
                    dangerous=getattr(skill, "dangerous", False),
                    expensive=getattr(skill, "expensive", False),
                )
                loaded.append(name)

        if loaded:
            # Check for new dependency failures
            failures = self.check_dependencies()
            if failures:
                for f in failures:
                    log.warning("Dependency check after hot_load: %s", f)

        return loaded

    # ── loading ────────────────────────────────────────────────────────────

    def load_builtins(
        self,
        db: Database | None = None,
        vector_memory: VectorMemory | None = None,
        project_store: ProjectStore | None = None,
        project_ctx: ProjectContextLoader | None = None,
        routing_engine: RoutingEngine | None = None,
        instruction_loader: InstructionLoader | None = None,
        shell_config: ShellConfig | None = None,
    ) -> None:
        """Import and register all built-in skills."""
        from src.skills.builtin.files import (
            ListFilesSkill,
            ReadFileSkill,
            WriteFileSkill,
        )
        from src.skills.builtin.media import GeneratePDFReport, SendVoiceNote
        from src.skills.builtin.planner import PlannerSkill
        from src.skills.builtin.routing import (
            RoutingAddSkill,
            RoutingDeleteSkill,
            RoutingListSkill,
        )
        from src.skills.builtin.shell import ShellSkill
        from src.skills.builtin.skills_manager import (
            SkillsAddSkill,
            SkillsFindSkill,
            SkillsListSkill,
            SkillsRemoveSkill,
        )
        from src.skills.builtin.task_scheduler import TaskSchedulerSkill
        from src.skills.builtin.web_research import WebResearchSkill

        self.register(WebResearchSkill())
        self.register(ShellSkill(shell_config))
        self.register(ReadFileSkill())
        self.register(WriteFileSkill())
        self.register(ListFilesSkill())
        self.register(PlannerSkill())
        self.register(TaskSchedulerSkill())
        self.register(SendVoiceNote())
        self.register(GeneratePDFReport())

        # Skills manager tools
        self.register(SkillsFindSkill())
        self.register(SkillsAddSkill())
        self.register(SkillsListSkill())
        self.register(SkillsRemoveSkill())

        # Routing skills — require routing_engine + instruction_loader
        if routing_engine is not None and instruction_loader is not None:
            self.register(RoutingListSkill(routing_engine))
            self.register(RoutingAddSkill(routing_engine, instruction_loader))
            self.register(RoutingDeleteSkill(routing_engine, instruction_loader))

        # Vector memory skills — always registered so the LLM can use them.
        # When vector_memory is None the skills degrade to text-based search
        # over MEMORY.md instead of being completely unavailable.
        from src.skills.builtin.memory_vss import (
            MemoryListSkill,
            MemorySaveSkill,
            MemorySearchSkill,
        )

        self.register(MemorySaveSkill(vector_memory))
        self.register(MemorySearchSkill(vector_memory))
        self.register(MemoryListSkill(vector_memory))

        # Project & Knowledge skills — reuse shared graph/recall from project_ctx
        if project_store is not None:
            from src.skills.builtin.project_skills import (
                KnowledgeAddSkill,
                KnowledgeLinkSkill,
                KnowledgeListSkill,
                KnowledgeSearchSkill,
                ProjectArchiveSkill,
                ProjectCreateSkill,
                ProjectInfoSkill,
                ProjectListSkill,
                ProjectRecallSkill,
                ProjectUpdateSkill,
            )

            # Share graph/recall instances with ProjectContextLoader to avoid duplicates
            if project_ctx is not None:
                graph = project_ctx.graph
                recall = project_ctx.recall
            else:
                from src.project.graph import ProjectGraph
                from src.project.recall import ProjectRecall

                graph = ProjectGraph(project_store)
                recall = ProjectRecall(project_store, graph, vector_memory)

            self.register(ProjectCreateSkill(project_store))
            self.register(ProjectListSkill(project_store))
            self.register(ProjectInfoSkill(project_store, graph))
            self.register(ProjectUpdateSkill(project_store))
            self.register(ProjectArchiveSkill(project_store))
            self.register(KnowledgeAddSkill(recall, project_store))
            self.register(KnowledgeSearchSkill(recall, project_store))
            self.register(KnowledgeLinkSkill(project_store))
            self.register(KnowledgeListSkill(project_store))
            self.register(ProjectRecallSkill(recall))

    def load_user_skills(self, directory: str) -> None:
        """
        Scan *directory* for user skills:
          • *.py files → look for BaseSkill subclasses, instantiate them
          • */SKILL.md files → wrap as PromptSkill (markdown-based)
        """
        d = Path(directory)
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            log.debug("Created user skills directory: %s", d)
            return

        # Python skills
        for py_file in sorted(d.glob("**/*.py")):
            self._load_python_skill(py_file)

        # Markdown prompt skills (skills.sh-style SKILL.md)
        for md_file in sorted(d.glob("**/SKILL.md")):
            self._load_markdown_skill(md_file)

    def _load_python_skill(self, path: Path) -> None:
        log.warning(
            "Loading user skill from %s — user skills execute arbitrary Python "
            "with the same privileges as the bot process. Only load trusted skills.",
            path,
        )
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)

            # Install a restricted __builtins__ to catch accidental
            # use of dangerous builtins during module loading.
            _orig_builtins = module.__dict__.get("__builtins__")
            module.__builtins__ = self._restricted_builtins()

            try:
                spec.loader.exec_module(module)  # type: ignore[arg-type]
            finally:
                # Restore original builtins so the skill can function normally
                module.__builtins__ = _orig_builtins

            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseSkill)
                    and obj is not BaseSkill
                    and not inspect.isabstract(obj)
                ):
                    # Validate the skill exposes the required interface
                    if not callable(getattr(obj, "execute", None)):
                        log.warning(
                            "Skipping skill %s from %s: missing callable execute()",
                            obj.__name__,
                            path,
                        )
                        continue

                    # Check manifest if present
                    skill_dir = path.parent
                    manifest = self._check_manifest(skill_dir)
                    if manifest is None:
                        # No manifest or manifest invalid → skip with warning
                        if (skill_dir / "manifest.json").is_file():
                            log.warning(
                                "Skipping skill %s from %s: manifest validation failed",
                                obj.__name__,
                                path,
                            )
                            continue

                    log.info(
                        "Loaded user skill: %s from %s",
                        obj.__name__,
                        path,
                    )
                    instance = obj()
                    if manifest:
                        self.register_skill(
                            instance,
                            version=manifest.version,
                            dependencies=[d.name for d in manifest.dependencies],
                        )
                    else:
                        self.register(instance)
        except Exception as exc:
            log.error("Failed to load skill from %s: %s", path, exc)

    def _check_manifest(self, skill_dir: Path) -> Optional["SkillManifest"]:
        """Load and validate a manifest; return None if absent or invalid."""
        from src.skills.manifest import (
            SkillManifest,
            check_dependencies as check_manifest_deps,
            load_manifest,
        )

        manifest = load_manifest(skill_dir)
        if manifest is None:
            return None

        # Validate dependencies against currently registered skills
        missing = check_manifest_deps(manifest, self.version_map)
        if missing:
            for dep_name in missing:
                log.warning(
                    "Skill %s has unmet dependency: %s",
                    manifest.name,
                    dep_name,
                )
            return None

        return manifest

    @staticmethod
    def _restricted_builtins() -> dict:
        """Create a restricted __builtins__ dict for user skill loading.

        Removes exec, eval, compile, and __import__ to reduce the attack
        surface during skill module loading. Skills can still import normally
        after loading because __builtins__ is restored.
        """
        import builtins as _builtins

        safe = dict(vars(_builtins))
        for name in ("exec", "eval", "compile", "__import__"):
            safe.pop(name, None)
        return safe

    def _load_markdown_skill(self, path: Path) -> None:
        from src.skills.prompt_skill import PromptSkill

        try:
            skill = PromptSkill.from_file(path)
            self.register(skill)
        except Exception as exc:
            log.error("Failed to load markdown skill from %s: %s", path, exc)

    # ── access ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[BaseSkill]:
        skill = self._skills.get(name)
        if skill is not None:
            self._lookup_hits += 1
        else:
            self._lookup_misses += 1
        return skill

    def all(self) -> List[BaseSkill]:
        return list(self._skills.values())

    @cached_property
    def tool_definitions(self) -> List[dict]:
        """
        Cached list of tool definitions for all registered skills.

        This property is computed once and cached until skills are
        modified (via register()). Use this instead of calling
        get_tool_definitions() repeatedly.
        """
        return [s.tool_definition for s in self._skills.values()]

    def get_tool_definitions(self) -> List[dict]:
        """Deprecated: Use tool_definitions property instead."""
        warnings.warn(
            "get_tool_definitions() is deprecated — use .tool_definitions property",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.tool_definitions

    def list_names(self) -> List[str]:
        return list(self._skills.keys())

    @property
    def cache_stats(self) -> Dict[str, int]:
        """Return skill-lookup cache hit/miss counters.

        Useful for monitoring and diagnostics — a low hit ratio suggests
        the LLM is frequently requesting non-existent tools.
        """
        return {
            "hits": self._lookup_hits,
            "misses": self._lookup_misses,
        }


__all__ = [
    "SkillRegistry",
    "SkillMeta",
    "BaseSkill",
    "SkillSandbox",
    "SandboxConfig",
    "SandboxViolation",
]
