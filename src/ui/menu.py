"""
src/ui/menu.py — Interactive /menu command builder.

Builds a formatted list of available skills grouped by category,
returned when the user sends ``/menu``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.skills import SkillRegistry

# Skill name prefixes → display categories.
_CATEGORIES: list[tuple[str, str]] = [
    ("read_file|write_file|list_files", "📁 File Ops"),
    ("web_research|web_search", "🌐 Web"),
    ("memory_save|memory_search|memory_list", "🧠 Memory"),
    ("shell|planner|task_scheduler", "⚙️ System"),
    ("send_voice|generate_pdf|project_|knowledge_", "💬 Communication"),
]

_DEFAULT_CATEGORY = "🔧 Tools"


def _categorize(name: str) -> str:
    """Return the display category for a skill *name*."""
    for pattern, label in _CATEGORIES:
        if any(p in name for p in pattern.split("|")):
            return label
    return _DEFAULT_CATEGORY


def format_skill_entry(name: str, description: str) -> str:
    """Format a single skill as a menu entry."""
    short_desc = description.split(".")[0] if description else "No description"
    return f"  /{name} — {short_desc}"


def build_menu(registry: SkillRegistry) -> str:
    """Build a categorized menu of all registered skills.

    Args:
        registry: The skill registry to enumerate.

    Returns:
        Formatted menu string ready to send to the user.
    """
    skills = registry.all()
    if not skills:
        return "No skills available."

    # Group skills by category.
    groups: dict[str, list[tuple[str, str]]] = {}
    for skill in skills:
        cat = _categorize(skill.name)
        groups.setdefault(cat, []).append((skill.name, skill.description))

    # Build output.
    lines: list[str] = ["*📋 Available Commands*", ""]
    for category in sorted(groups):
        lines.append(category)
        for name, desc in groups[category]:
            lines.append(format_skill_entry(name, desc))
        lines.append("")

    lines.append("Send any command name to use it.")
    return "\n".join(lines)


def is_menu_command(text: str) -> bool:
    """Return True if *text* is a /menu command."""
    return text.strip().lower() == "/menu"


class CommandMenu:
    """Build and serve the interactive command menu."""

    def __init__(self, registry: SkillRegistry) -> None:
        self._registry = registry

    def render(self) -> str:
        """Render the full menu."""
        return build_menu(self._registry)

    def handle(self, text: str) -> str | None:
        """Return menu text if *text* is /menu, else None."""
        if is_menu_command(text):
            return self.render()
        return None
