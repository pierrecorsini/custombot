"""
src/skills/prompt_skill.py — Markdown-based "prompt skills" (picoclaw-style).

A skill.md file at  workspace/skills/<skill-name>/skill.md  describes what the
skill does.  The model reads the description and calls the skill with a
single `input` argument; the skill spins up a sub-LLM call using the
markdown instructions as the system prompt and returns the result.

Example skill.md:
─────────────────────────────────────────────────────────
# Summarize

Summarize the given text in three bullet points.
Return ONLY the bullet points, nothing else.

## Parameters
- input: The text to summarize
─────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from src.skills.base import BaseSkill


class PromptSkill(BaseSkill):
    """Wraps a skill.md file as a callable LLM-powered tool."""

    parameters = {
        "type": "object",
        "properties": {
            "input": {
                "type": "string",
                "description": "The input text or request for this skill.",
            }
        },
        "required": ["input"],
    }

    def __init__(self, name: str, description: str, system_prompt: str) -> None:
        self.name = name
        self.description = description
        self._system_prompt = system_prompt
        # LLM client will be injected via wire_llm() by the registry
        self._llm: Any = None

    @classmethod
    def from_file(cls, path: Path) -> "PromptSkill":
        content = path.read_text(encoding="utf-8").strip()
        # Derive name from the parent directory name
        name = _to_tool_name(path.parent.name)
        # First heading → description; rest → system prompt
        lines = content.splitlines()
        description = ""
        for i, line in enumerate(lines):
            stripped = line.strip("# ").strip()
            if stripped:
                description = stripped
                break
        system_prompt = content
        return cls(name=name, description=description, system_prompt=system_prompt)

    def needs_llm(self) -> bool:
        return True

    def wire_llm(self, llm: Any) -> None:
        self._llm = llm

    async def execute(self, workspace_dir: Path, input: str = "", **kwargs: Any) -> str:
        if self._llm is None:
            return "[PromptSkill: LLM client not attached]"
        messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": input},
        ]
        response = await self._llm.chat(messages)
        return response.choices[0].message.content or ""


def _to_tool_name(raw: str) -> str:
    """Convert a directory name to a valid tool function name."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", raw).strip("_").lower()
    return name or "prompt_skill"
