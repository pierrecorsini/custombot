"""
src/skills/builtin/skills_manager.py — Manage skills.sh skills.

Provides LLM-callable tools for discovering and installing skills from
the skills.sh ecosystem. Skills are installed to workspace/skills/ directory.

Tools:
  • SkillsFindSkill   — Search skills.sh for relevant skills
  • SkillsAddSkill    — Install a skill from skills.sh
  • SkillsListSkill   — List installed skills in workspace/skills/
  • SkillsRemoveSkill — Remove an installed skill
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any, List, Optional

from src.constants import WORKSPACE_DIR
from src.skills.base import BaseSkill
from src.utils.async_executor import AsyncExecutor

# Directory where user skills are installed (relative to project root)
USER_SKILLS_DIR = Path(__file__).parent.parent.parent / WORKSPACE_DIR / "skills"


class SkillsFindSkill(BaseSkill):
    """Search skills.sh for relevant skills."""

    name = "skills_find"
    description = (
        "Search the skills.sh ecosystem for skills that match a query. "
        "Returns a list of available skills with their install commands. "
        "Use this when the user asks 'how do I do X' or 'is there a skill for X'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Search keywords (e.g., 'web search', 'calendar', 'testing'). "
                    "Use specific terms for better results."
                ),
            },
        },
        "required": ["query"],
    }

    async def execute(
        self,
        workspace_dir: Path,
        query: str = "",
        **kwargs: Any,
    ) -> str:
        if not query:
            return "❌ Error: Please provide a search query."

        try:
            executor = AsyncExecutor(timeout=30.0)
            result = await executor.run(["npx", "skills", "find", query])

            if result.timed_out:
                return "❌ Search timed out. Please try again."

            if not result.success and not result.stdout:
                return f"❌ Search failed: {result.stderr or 'Unknown error'}"

            if not result.stdout:
                return f"🔍 No skills found for '{query}'. Try different keywords."

            return f"🔍 **Skills matching '{query}':**\n\n{result.stdout.strip()}"

        except FileNotFoundError:
            return "❌ Error: 'npx' command not found. Please install Node.js."
        except Exception as e:
            return f"❌ Error searching skills: {e}"


class SkillsAddSkill(BaseSkill):
    """Install a skill from skills.sh to workspace/skills/."""

    name = "skills_add"
    description = (
        "Install a skill from the skills.sh ecosystem. "
        "Skills are installed to the workspace/skills/ directory. "
        "Use the package format: 'owner/repo@skill-name' (e.g., 'chaterm/terminal-skills@cron')."
    )
    parameters = {
        "type": "object",
        "properties": {
            "package": {
                "type": "string",
                "description": (
                    "Skill package to install (e.g., 'chaterm/terminal-skills@cron', "
                    "'inferen-sh/skills@web-search'). Use the format shown in skills_find results."
                ),
            },
        },
        "required": ["package"],
    }

    async def execute(
        self,
        workspace_dir: Path,
        package: str = "",
        **kwargs: Any,
    ) -> str:
        if not package:
            return "❌ Error: Please provide a package name (e.g., 'owner/repo@skill')."

        # Ensure workspace/skills directory exists
        USER_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

        try:
            executor = AsyncExecutor(timeout=60.0)
            result = await executor.run(["npx", "skills", "add", package, "-y"])

            if result.timed_out:
                return "❌ Installation timed out. Please try again."

            if not result.success:
                return f"❌ Failed to install skill: {result.stderr or result.stdout or 'Unknown error'}"

            return f"✅ Skill installed successfully!\n\n{result.stdout.strip()}\n\nUse `skills_list` to see installed skills."

        except FileNotFoundError:
            return "❌ Error: 'npx' command not found. Please install Node.js."
        except Exception as e:
            return f"❌ Error installing skill: {e}"


class SkillsListSkill(BaseSkill):
    """List builtin and user-installed skills."""

    name = "skills_list"
    description = "List all available skills: builtin skills and user-installed skills."
    parameters = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(
        self,
        workspace_dir: Path,
        **kwargs: Any,
    ) -> str:
        builtin = self._list_builtin_skills()
        user = self._list_user_skills()
        return f"{builtin}\n\n{user}"

    def _list_builtin_skills(self) -> str:
        """Extract skills from builtin Python files."""
        builtin_dir = Path(__file__).parent
        skills = []

        for py_file in sorted(builtin_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            for name, desc in self._extract_skills_from_file(py_file):
                skills.append(f"• **{name}** — {desc}")

        if not skills:
            return "📦 **Builtin Skills**: None"
        return "📦 **Builtin Skills**:\n" + "\n".join(skills)

    def _extract_skills_from_file(self, path: Path) -> List[tuple]:
        """Parse Python file for skill classes with name/description."""
        import ast

        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            return []

        skills = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                name_val, desc_val = None, None
                for item in node.body:
                    if isinstance(item, ast.Assign):
                        for target in item.targets:
                            if isinstance(target, ast.Name):
                                if target.id == "name" and isinstance(item.value, ast.Constant):
                                    name_val = item.value.value
                                elif target.id == "description" and isinstance(
                                    item.value, ast.Constant
                                ):
                                    desc_val = item.value.value
                if name_val:
                    skills.append((name_val, desc_val or ""))
        return skills

    def _list_user_skills(self) -> str:
        """List skills from workspace/skills/ directory."""
        if not USER_SKILLS_DIR.exists():
            return "📁 **User Skills**: None installed. Use `skills_find` to discover."

        skills = []
        for skill_dir in sorted(USER_SKILLS_DIR.iterdir()):
            if skill_dir.is_dir():
                skill_md = skill_dir / "SKILL.md"
                if skill_md.exists():
                    name, desc = self._parse_skill_md(skill_md, skill_dir.name)
                    skills.append(f"• **{name}** — {desc}")

        if not skills:
            return "📁 **User Skills**: None installed. Use `skills_find` to discover."
        return "📁 **User Skills**:\n" + "\n".join(skills)

    def _parse_skill_md(self, path: Path, default_name: str) -> tuple:
        """Extract name and description from SKILL.md frontmatter."""
        try:
            content = path.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    name, desc = default_name, ""
                    for line in parts[1].strip().splitlines():
                        if line.startswith("name:"):
                            name = line.split(":", 1)[1].strip().strip("\"'")
                        elif line.startswith("description:"):
                            desc = line.split(":", 1)[1].strip().strip("\"'")
                    return name, desc
        except Exception:
            pass
        return default_name, ""


class SkillsRemoveSkill(BaseSkill):
    """Remove a skill from workspace/skills/."""

    name = "skills_remove"
    description = (
        "Remove a skill from the workspace/skills/ directory. "
        "Provide the skill directory name (use skills_list to see names)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Name of the skill to remove (the directory name in workspace/skills/). "
                    "Use skills_list to see available skills."
                ),
            },
        },
        "required": ["name"],
    }

    async def execute(
        self,
        workspace_dir: Path,
        name: str = "",
        **kwargs: Any,
    ) -> str:
        if not name:
            return "❌ Error: Please provide the skill name to remove."

        # Sanitize the name to prevent path traversal
        safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        skill_dir = USER_SKILLS_DIR / safe_name

        if not skill_dir.exists():
            return f"❌ Skill '{name}' not found in workspace/skills/. Use `skills_list` to see installed skills."

        # Safety check: make sure it's actually in workspace/skills/
        try:
            skill_dir.resolve().relative_to(USER_SKILLS_DIR.resolve())
        except ValueError:
            return "❌ Error: Invalid skill path."

        try:
            shutil.rmtree(skill_dir)
            return f"✅ Skill '{name}' removed successfully."
        except Exception as e:
            return f"❌ Error removing skill: {e}"
