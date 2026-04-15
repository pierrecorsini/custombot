"""
src/skills/__init__.py — Skill registry and dynamic loader.

Loads skills from two sources:
  1. Built-in Python skills  (src/skills/builtin/)
  2. User-authored skills    (workspace/skills/)
       • Python files that define a class inheriting BaseSkill
       • Markdown SKILL.md files (prompt skills, skills.sh-style)

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
from functools import cached_property
from pathlib import Path
from typing import Dict, List, Optional

from src.skills.base import BaseSkill

log = logging.getLogger(__name__)


class SkillRegistry:
    def __init__(self) -> None:
        self._skills: Dict[str, BaseSkill] = {}

    # ── registration ───────────────────────────────────────────────────────

    def register(self, skill: BaseSkill) -> None:
        if not skill.name:
            log.warning("Skill %r has no name, skipping.", type(skill).__name__)
            return
        self._skills[skill.name] = skill
        # Invalidate cached tool definitions when skills change
        self.__dict__.pop("tool_definitions", None)
        log.debug("Registered skill: %s", skill.name)

    # ── loading ────────────────────────────────────────────────────────────

    def load_builtins(
        self,
        db=None,
        vector_memory=None,
        project_store=None,
        project_ctx=None,
        routing_engine=None,
        instruction_loader=None,
    ) -> None:
        """Import and register all built-in skills."""
        from src.skills.builtin.web_research import WebResearchSkill
        from src.skills.builtin.shell import ShellSkill
        from src.skills.builtin.files import (
            ReadFileSkill,
            WriteFileSkill,
            ListFilesSkill,
        )
        from src.skills.builtin.routing import (
            RoutingListSkill,
            RoutingAddSkill,
            RoutingDeleteSkill,
        )
        from src.skills.builtin.skills_manager import (
            SkillsFindSkill,
            SkillsAddSkill,
            SkillsListSkill,
            SkillsRemoveSkill,
        )
        from src.skills.builtin.planner import PlannerSkill
        from src.skills.builtin.task_scheduler import TaskSchedulerSkill
        from src.skills.builtin.media import SendVoiceNote, GeneratePDFReport

        self.register(WebResearchSkill())
        self.register(ShellSkill())
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

        # Vector memory skills
        if vector_memory is not None:
            from src.skills.builtin.memory_vss import (
                MemorySaveSkill,
                MemorySearchSkill,
                MemoryListSkill,
            )

            self.register(MemorySaveSkill(vector_memory))
            self.register(MemorySearchSkill(vector_memory))
            self.register(MemoryListSkill(vector_memory))

        # Project & Knowledge skills — reuse shared graph/recall from project_ctx
        if project_store is not None:
            from src.skills.builtin.project_skills import (
                ProjectCreateSkill,
                ProjectListSkill,
                ProjectInfoSkill,
                ProjectUpdateSkill,
                ProjectArchiveSkill,
                KnowledgeAddSkill,
                KnowledgeSearchSkill,
                KnowledgeLinkSkill,
                KnowledgeListSkill,
                ProjectRecallSkill,
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
        try:
            spec = importlib.util.spec_from_file_location(path.stem, path)
            if spec is None or spec.loader is None:
                return
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)  # type: ignore[arg-type]
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(obj, BaseSkill)
                    and obj is not BaseSkill
                    and not inspect.isabstract(obj)
                ):
                    self.register(obj())
        except Exception as exc:
            log.error("Failed to load skill from %s: %s", path, exc)

    def _load_markdown_skill(self, path: Path) -> None:
        from src.skills.prompt_skill import PromptSkill

        try:
            skill = PromptSkill.from_file(path)
            self.register(skill)
        except Exception as exc:
            log.error("Failed to load markdown skill from %s: %s", path, exc)

    # ── access ─────────────────────────────────────────────────────────────

    def get(self, name: str) -> Optional[BaseSkill]:
        return self._skills.get(name)

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
        return self.tool_definitions

    def list_names(self) -> List[str]:
        return list(self._skills.keys())
