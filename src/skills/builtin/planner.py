"""
src/skills/builtin/planner.py — Task planning skill.

Creates plans, manages dependencies, tracks progress.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from src.skills.base import BaseSkill, validate_input
from src.utils import JSONDecodeError, json_loads

log = logging.getLogger(__name__)

PLANS_DIR = ".plans"


class PlannerSkill(BaseSkill):
    name = "planner"
    description = (
        "Plan and track tasks. Create plans, add tasks with dependencies, "
        "view execution order, mark complete."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["init", "add", "list", "next", "complete", "status", "plan"],
                "description": "Action: init, add, list, next, complete, status, plan",
            },
            "name": {"type": "string", "description": "Plan name"},
            "title": {"type": "string", "description": "Task title (for add)"},
            "deps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Task dependencies (task IDs)",
            },
            "task_id": {"type": "string", "description": "Task ID to complete"},
            "summary": {"type": "string", "description": "Completion summary"},
        },
        "required": ["action"],
    }

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        action: str = "",
        name: str = "",
        title: str = "",
        deps: list[str] | None = None,
        task_id: str = "",
        summary: str = "",
        **kwargs: Any,
    ) -> str:
        self._plans_dir = workspace_dir / PLANS_DIR
        self._plans_dir.mkdir(exist_ok=True)

        actions = {
            "init": lambda: self._init(name, title),
            "add": lambda: self._add(name, title, deps or []),
            "list": lambda: self._list(),
            "next": lambda: self._next(name),
            "complete": lambda: self._complete(name, task_id, summary),
            "status": lambda: self._status(name),
            "plan": lambda: self._plan(name),
        }

        fn = actions.get(action)
        if not fn:
            return f"Unknown action: {action}. Use: {', '.join(actions)}"

        return fn()

    def _load(self, name: str) -> dict:
        """Load plan from file."""
        path = self._plans_dir / f"{name}.json"
        if not path.exists():
            return {}
        try:
            return json_loads(path.read_text(encoding="utf-8"))
        except (JSONDecodeError, OSError) as exc:
            log.error("Failed to load plan %s: %s", name, exc)
            return {}

    def _save(self, name: str, data: dict) -> None:
        """Save plan to file."""
        path = self._plans_dir / f"{name}.json"
        data["updated"] = datetime.now().isoformat()
        try:
            path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            log.error("Failed to save plan %s: %s", name, exc)

    def _init(self, name: str, desc: str) -> str:
        """Create a new plan."""
        if not name:
            return "Error: plan name required"

        path = self._plans_dir / f"{name}.json"
        if path.exists():
            return f"Plan '{name}' already exists"

        self._save(
            name,
            {
                "name": name,
                "desc": desc,
                "tasks": [],
                "created": datetime.now().isoformat(),
            },
        )
        return f"Created plan '{name}'"

    def _add(self, name: str, title: str, deps: list[str]) -> str:
        """Add a task to a plan."""
        data = self._load(name)
        if not data:
            return f"Plan '{name}' not found. Create it first."

        tasks = data.get("tasks", [])
        task_id = str(len(tasks) + 1).zfill(2)

        tasks.append(
            {
                "id": task_id,
                "title": title,
                "deps": deps,
                "status": "pending",
                "summary": None,
            }
        )
        data["tasks"] = tasks
        self._save(name, data)

        return f"Added task {task_id}: {title}"

    def _list(self) -> str:
        """List all plans."""
        plans = list(self._plans_dir.glob("*.json"))
        if not plans:
            return "No plans found. Create one with: init <name>"

        lines = ["## Plans\n"]
        for p in plans:
            try:
                data = json_loads(p.read_text(encoding="utf-8"))
                tasks = data.get("tasks", [])
                done = sum(1 for t in tasks if t.get("status") == "done")
                total = len(tasks)
                lines.append(f"- **{data['name']}** ({done}/{total} done)")
            except (JSONDecodeError, KeyError) as exc:
                log.warning("Skipping corrupt plan file %s: %s", p.name, exc)

        return "\n".join(lines)

    def _next(self, name: str) -> str:
        """Show next eligible tasks."""
        data = self._load(name)
        if not data:
            return f"Plan '{name}' not found"

        done = {t["id"] for t in data.get("tasks", []) if t.get("status") == "done"}
        ready = []

        for t in data.get("tasks", []):
            if t.get("status") == "done":
                continue
            if all(d in done for d in t.get("deps", [])):
                ready.append(t)

        if not ready:
            return "No tasks ready. Complete blocking tasks first."

        lines = ["## Ready Tasks\n"]
        for t in ready:
            lines.append(f"- {t['id']}: {t['title']}")
        return "\n".join(lines)

    def _complete(self, name: str, task_id: str, summary: str) -> str:
        """Mark a task complete."""
        data = self._load(name)
        if not data:
            return f"Plan '{name}' not found"

        for t in data.get("tasks", []):
            if t["id"] == task_id:
                t["status"] = "done"
                t["summary"] = summary
                self._save(name, data)

                done = sum(1 for x in data["tasks"] if x.get("status") == "done")
                return f"Completed {task_id}. Progress: {done}/{len(data['tasks'])}"

        return f"Task {task_id} not found in plan '{name}'"

    def _status(self, name: str) -> str:
        """Show plan status."""
        data = self._load(name)
        if not data:
            return f"Plan '{name}' not found"

        tasks = data.get("tasks", [])
        done = sum(1 for t in tasks if t.get("status") == "done")
        total = len(tasks)

        lines = [f"## {name}\n", f"**Progress**: {done}/{total}"]
        for t in tasks:
            icon = "✓" if t.get("status") == "done" else "○"
            lines.append(f"  {icon} {t['id']}: {t['title']}")

        return "\n".join(lines)

    def _plan(self, name: str) -> str:
        """Show execution batches."""
        data = self._load(name)
        if not data:
            return f"Plan '{name}' not found"

        tasks = {t["id"]: t for t in data.get("tasks", [])}
        batches = []
        placed = set()

        while len(placed) < len(tasks):
            batch = []
            for tid, t in tasks.items():
                if tid in placed:
                    continue
                if all(d in placed for d in t.get("deps", [])):
                    batch.append(t)
            if not batch:
                break
            for t in batch:
                placed.add(t["id"])
            batches.append(batch)

        lines = ["## Execution Plan\n"]
        for i, batch in enumerate(batches, 1):
            lines.append(f"**Batch {i}**:")
            for t in batch:
                lines.append(f"  - {t['id']}: {t['title']}")

        return "\n".join(lines)
