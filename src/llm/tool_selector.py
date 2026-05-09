"""
src/llm/tool_selector.py — Dynamic tool selection to reduce token waste.

Analyzes the user message for domain keywords and includes only relevant
tool definitions in the LLM request.  Falls back to all tools when no
domain matches are found.
"""

from __future__ import annotations

import logging
import re
from typing import Any

if TYPE_CHECKING:
    from src.skills import SkillRegistry

log = logging.getLogger(__name__)

# ── Domain definitions ──────────────────────────────────────────────────────

_ALWAYS_INCLUDE: frozenset[str] = frozenset({"think"})

_DOMAINS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "file_ops": (
        frozenset({"read_file", "write_file", "list_files"}),
        frozenset({"file", "read", "write", "create", "delete", "list", "path", "folder", "directory"}),
    ),
    "shell": (
        frozenset({"shell"}),
        frozenset({"run", "execute", "command", "shell", "bash", "script", "install", "pip", "npm"}),
    ),
    "web": (
        frozenset({"web_research"}),
        frozenset({"search", "web", "internet", "lookup", "find", "google", "browse", "url", "http"}),
    ),
    "memory": (
        frozenset({"memory_save", "memory_search", "memory_list"}),
        frozenset({"remember", "memory", "note", "recall", "forget"}),
    ),
    "task": (
        frozenset({"task_scheduler"}),
        frozenset({"schedule", "remind", "later", "timer", "alarm", "task"}),
    ),
    "project": (
        frozenset({
            "project_create", "project_list", "project_info",
            "project_update", "project_archive",
            "knowledge_add", "knowledge_search", "knowledge_link", "knowledge_list",
            "project_recall",
        }),
        frozenset({"project", "knowledge", "graph", "wiki"}),
    ),
    "skills_mgmt": (
        frozenset({"skills_find", "skills_add", "skills_list", "skills_remove"}),
        frozenset({"skill", "plugin", "extension", "install skill"}),
    ),
    "routing": (
        frozenset({"routing_list", "routing_add", "routing_delete"}),
        frozenset({"route", "routing", "rule", "instruction"}),
    ),
}


def _match_domains(message: str) -> set[str]:
    """Return domain names whose keywords appear in *message*."""
    lower = message.lower()
    matched: set[str] = set()
    for domain, (_, keywords) in _DOMAINS.items():
        for kw in keywords:
            if re.search(rf"\b{re.escape(kw)}\b", lower):
                matched.add(domain)
                break
    return matched


def select_tools(
    registry: SkillRegistry,
    user_message: str,
    enabled: bool = True,
) -> list[dict[str, Any]]:
    """Return tool definitions relevant to *user_message*.

    When *enabled* is ``False`` or no domain matches, returns all tools
    (current behaviour).  The ``think`` skill is always included when present.
    """
    if not enabled:
        return registry.tool_definitions

    matched = _match_domains(user_message)
    if not matched:
        log.debug("No domain match — including all tools")
        return registry.tool_definitions

    wanted_skills: set[str] = set(_ALWAYS_INCLUDE)
    for domain in matched:
        skill_names, _ = _DOMAINS[domain]
        wanted_skills.update(skill_names)

    tools: list[dict[str, Any]] = []
    for skill in registry.all():
        if skill.name in wanted_skills:
            tools.append(skill.tool_definition)

    if not tools:
        return registry.tool_definitions

    log.debug(
        "Dynamic tool selection: %d/%d tools for domains %s",
        len(tools),
        len(registry.tool_definitions),
        sorted(matched),
    )
    return tools
