"""
src/skills/builtin/project_skills.py — Project & Knowledge management skills.

10 LLM-callable tools:
  Project:  project_create, project_list, project_info, project_update, project_archive
  Knowledge: knowledge_add, knowledge_search, knowledge_link, knowledge_list, project_recall

Uses _ProjectSkillBase / _KnowledgeSkillBase to eliminate boilerplate
for dependency injection and not-found checks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from src.skills.base import BaseSkill, validate_input
from src.project.store import ProjectStore
from src.project.graph import ProjectGraph
from src.project.recall import ProjectRecall
from src.project.dates import fmt_ts

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# BASE CLASSES
# ═══════════════════════════════════════════════════════════════════════════


class _ProjectSkillBase(BaseSkill):
    """Base for skills that need a ProjectStore dependency."""

    def __init__(self, store: ProjectStore) -> None:
        self._store = store

    def _project_or_error(self, pid: str) -> tuple[Optional[dict], Optional[str]]:
        """Return (project, None) or (None, error_message)."""
        project = self._store.get_project(pid)
        if not project:
            return None, f"Project '{pid}' not found."
        return project, None

    def _entry_or_error(self, entry_id: int) -> tuple[Optional[dict], Optional[str]]:
        """Return (entry, None) or (None, error_message)."""
        entry = self._store.get_knowledge(entry_id)
        if not entry:
            return None, f"Entry {entry_id} not found."
        return entry, None


class _KnowledgeSkillBase(_ProjectSkillBase):
    """Base for skills that need both ProjectStore and ProjectRecall."""

    def __init__(self, recall: ProjectRecall, store: ProjectStore) -> None:
        super().__init__(store)
        self._recall = recall


# ═══════════════════════════════════════════════════════════════════════════
# PROJECT SKILLS
# ═══════════════════════════════════════════════════════════════════════════


class ProjectCreateSkill(_ProjectSkillBase):
    name = "project_create"
    description = (
        "Create a new project to organize and track knowledge, decisions, and facts. "
        "Projects act as containers for related knowledge entries."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Project name (e.g. 'website-redesign', 'mobile-app').",
            },
            "description": {
                "type": "string",
                "description": "Brief description of the project.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for categorization.",
            },
        },
        "required": ["name"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        name = kwargs["name"]
        desc = kwargs.get("description", "")
        tags = kwargs.get("tags", [])
        project = self._store.create_project(name=name, description=desc, tags=tags)
        return f"Created project '{project['name']}' (id: {project['id']}, status: {project['status']})"


class ProjectListSkill(_ProjectSkillBase):
    name = "project_list"
    description = (
        "List all projects, optionally filtered by status or tag. "
        "Use this to find existing projects before adding knowledge."
    )
    parameters = {
        "type": "object",
        "properties": {
            "status": {
                "type": "string",
                "description": "Filter by status: active, paused, completed, archived.",
            },
            "tag": {
                "type": "string",
                "description": "Filter by tag.",
            },
        },
        "required": [],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        status = kwargs.get("status")
        tag = kwargs.get("tag")
        projects = self._store.list_projects(status=status, tag=tag)
        if not projects:
            return "No projects found."
        lines = [f"Found {len(projects)} project(s):\n"]
        for p in projects:
            kcount = self._store.count_knowledge(p["id"])
            tags_str = f" [{', '.join(p['tags'])}]" if p["tags"] else ""
            lines.append(
                f"- **{p['name']}** (id: {p['id']}) status={p['status']}{tags_str} "
                f"knowledge={kcount}"
            )
        return "\n".join(lines)


class ProjectInfoSkill(BaseSkill):
    """Detailed project info — needs graph in addition to store."""

    name = "project_info"
    description = (
        "Get detailed information about a project, including recent knowledge entries, "
        "tags, status, and linked chats."
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project identifier.",
            },
            "include_knowledge": {
                "type": "boolean",
                "description": "Include recent knowledge entries (default: true).",
            },
        },
        "required": ["project_id"],
    }

    def __init__(self, store: ProjectStore, graph: ProjectGraph) -> None:
        self._store = store
        self._graph = graph

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        project = self._store.get_project(pid)
        if not project:
            return f"Project '{pid}' not found."
        lines = [
            f"## {project['name']}",
            f"**ID**: {project['id']}",
            f"**Status**: {project['status']}",
        ]
        if project["description"]:
            lines.append(f"**Description**: {project['description']}")
        if project["tags"]:
            lines.append(f"**Tags**: {', '.join(project['tags'])}")

        lines.append(f"**Created**: {fmt_ts(project['created_at'])}")
        lines.append(f"**Updated**: {fmt_ts(project['updated_at'])}")

        chats = self._store.get_project_chats(pid)
        if chats:
            lines.append(f"\n**Linked chats**: {len(chats)}")

        kcount = self._store.count_knowledge(pid)
        lines.append(f"\n**Knowledge entries**: {kcount}")

        if kwargs.get("include_knowledge", True) and kcount > 0:
            entries = self._store.list_knowledge(pid, limit=10)
            lines.append("\n### Recent Knowledge:\n")
            for e in entries:
                lines.append(
                    f"- [{e['category']}] (id:{e['id']}) "
                    f"{e.get('title') or e['text'][:80]}\n"
                    f"  {fmt_ts(e['created_at'])}"
                )

        return "\n".join(lines)


class ProjectUpdateSkill(_ProjectSkillBase):
    name = "project_update"
    description = (
        "Update a project's name, description, status, or tags. "
        "Use to pause, complete, or reactivate projects."
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project identifier.",
            },
            "name": {
                "type": "string",
                "description": "New project name.",
            },
            "description": {
                "type": "string",
                "description": "New description.",
            },
            "status": {
                "type": "string",
                "description": "New status: active, paused, completed, archived.",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "New tags (replaces existing).",
            },
        },
        "required": ["project_id"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        try:
            updated = self._store.update_project(
                project_id=pid,
                name=kwargs.get("name"),
                description=kwargs.get("description"),
                status=kwargs.get("status"),
                tags=kwargs.get("tags"),
            )
        except ValueError as e:
            return str(e)
        if not updated:
            return f"Project '{pid}' not found."
        return f"Updated project '{updated['name']}' (status: {updated['status']})"


class ProjectArchiveSkill(_ProjectSkillBase):
    name = "project_archive"
    description = (
        "Archive a project. Sets status to 'archived' — the project and its knowledge "
        "are preserved but hidden from default listings."
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project identifier to archive.",
            },
        },
        "required": ["project_id"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        updated = self._store.update_project(project_id=pid, status="archived")
        if not updated:
            return f"Project '{pid}' not found."
        return f"Archived project '{updated['name']}'."


# ═══════════════════════════════════════════════════════════════════════════
# KNOWLEDGE SKILLS
# ═══════════════════════════════════════════════════════════════════════════


class KnowledgeAddSkill(_KnowledgeSkillBase):
    name = "knowledge_add"
    description = (
        "Add a knowledge entry to a project. Stores a fact, decision, requirement, "
        "note, link, contact, or task. Optionally link it to an existing entry."
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project to add knowledge to.",
            },
            "text": {
                "type": "string",
                "description": "The knowledge content to store.",
            },
            "title": {
                "type": "string",
                "description": "Short title/summary for this entry.",
            },
            "category": {
                "type": "string",
                "description": "Type: decision, fact, requirement, note, link, contact, task.",
            },
            "link_to": {
                "type": "integer",
                "description": "ID of an existing entry to link this to.",
            },
            "link_relation": {
                "type": "string",
                "description": "Relation type: relates_to, depends_on, contradicts, supersedes, part_of, references.",
            },
        },
        "required": ["project_id", "text"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        project, err = self._project_or_error(pid)
        if err:
            return f"{err} Create it first with project_create."

        text = kwargs["text"]
        title = kwargs.get("title", "")
        category = kwargs.get("category", "note")
        link_to = kwargs.get("link_to")
        link_relation = kwargs.get("link_relation", "relates_to")

        try:
            entry = await self._recall.save_knowledge(
                project_id=pid,
                text=text,
                title=title,
                category=category,
                source_chat_id=workspace_dir.name,
                link_to=link_to,
                link_relation=link_relation,
            )
        except ValueError as e:
            return str(e)

        link_str = ""
        if link_to:
            link_str = f" (linked to entry {link_to} via {link_relation})"
        return (
            f"Added [{category}] to project '{project['name']}' "
            f"[id={entry['id']}]{link_str}: "
            f"{title or text[:80]}"
        )


class KnowledgeSearchSkill(_KnowledgeSkillBase):
    name = "knowledge_search"
    description = (
        "Search knowledge within a project using natural language. "
        "Combines semantic vector search with graph-based relationship discovery."
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project to search in.",
            },
            "query": {
                "type": "string",
                "description": "Natural language search query.",
            },
            "category": {
                "type": "string",
                "description": "Filter by category: decision, fact, requirement, note, etc.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results to return (default: 5).",
            },
        },
        "required": ["project_id", "query"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        project, err = self._project_or_error(pid)
        if err:
            return err

        query = kwargs["query"]
        category = kwargs.get("category")
        limit = kwargs.get("limit", 5)

        results = await self._recall.search(
            project_id=pid,
            query=query,
            category=category,
            limit=limit,
        )

        if not results:
            return f"No knowledge found in '{project['name']}' matching your query."

        lines = [f"Found {len(results)} result(s) in '{project['name']}':\n"]
        for r in results:
            relevance = r.get("relevance")
            rel_str = f" (relevance: {relevance:.2f})" if relevance else ""
            title = r.get("title", "")
            cat = r.get("category", "")
            lines.append(f"- [{cat}] {title}{rel_str}")
            lines.append(f"  {r['text'][:200]}")
            related = r.get("related", [])
            if related:
                for rel_entry in related[:3]:
                    lines.append(
                        f"  -> [{rel_entry.get('_relation', 'related')}] "
                        f"{rel_entry.get('title') or rel_entry['text'][:60]}"
                    )
        return "\n".join(lines)


class KnowledgeLinkSkill(_ProjectSkillBase):
    name = "knowledge_link"
    description = (
        "Create a relationship between two knowledge entries. "
        "Use to connect decisions, facts, or requirements."
    )
    parameters = {
        "type": "object",
        "properties": {
            "from_id": {
                "type": "integer",
                "description": "Source entry ID.",
            },
            "to_id": {
                "type": "integer",
                "description": "Target entry ID.",
            },
            "relation": {
                "type": "string",
                "description": "Relation: relates_to, depends_on, contradicts, supersedes, part_of, references.",
            },
        },
        "required": ["from_id", "to_id", "relation"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        from_id = kwargs["from_id"]
        to_id = kwargs["to_id"]
        relation = kwargs["relation"]

        _, err = self._entry_or_error(from_id)
        if err:
            return err
        _, err = self._entry_or_error(to_id)
        if err:
            return err

        try:
            link_id = self._store.link_knowledge(from_id, to_id, relation)
        except ValueError as e:
            return str(e)

        if link_id is None:
            return "Link already exists between those entries."
        return f"Linked entry {from_id} ->[{relation}]-> entry {to_id} (link_id: {link_id})"


class KnowledgeListSkill(_ProjectSkillBase):
    name = "knowledge_list"
    description = "List knowledge entries for a project. Optionally filter by category."
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project identifier.",
            },
            "category": {
                "type": "string",
                "description": "Filter by category: decision, fact, requirement, note, link, contact, task.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum entries to return (default: 15).",
            },
        },
        "required": ["project_id"],
    }

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        project, err = self._project_or_error(pid)
        if err:
            return err

        category = kwargs.get("category")
        limit = kwargs.get("limit", 15)

        entries = self._store.list_knowledge(pid, category=category, limit=limit)
        total = self._store.count_knowledge(pid)

        if not entries:
            return f"No knowledge entries in '{project['name']}'."

        cat_filter = f" (category: {category})" if category else ""
        lines = [
            f"Knowledge for '{project['name']}'{cat_filter} — showing {len(entries)}/{total}:\n"
        ]
        for e in entries:
            title = e.get("title") or e["text"][:60]
            lines.append(
                f"- [id:{e['id']}] [{e['category']}] {title}\n"
                f"  {fmt_ts(e['created_at'])}"
            )
            if e.get("title"):
                lines.append(f"  {e['text'][:150]}")
        return "\n".join(lines)


class ProjectRecallSkill(BaseSkill):
    name = "project_recall"
    description = (
        "Retrieve the full context of a project for injection into conversation. "
        "Returns a structured summary of all knowledge, decisions, and relationships. "
        "Use when starting a conversation about a project or when you need the full picture."
    )
    parameters = {
        "type": "object",
        "properties": {
            "project_id": {
                "type": "string",
                "description": "The project identifier to recall.",
            },
        },
        "required": ["project_id"],
    }

    def __init__(self, recall: ProjectRecall) -> None:
        self._recall = recall

    @validate_input
    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        pid = kwargs["project_id"]
        context = self._recall.recall(pid)
        if not context:
            return f"Project '{pid}' not found."
        return context
