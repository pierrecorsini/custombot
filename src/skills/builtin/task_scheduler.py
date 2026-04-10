"""
src/skills/builtin/task_scheduler.py — Task scheduling skill.

Exposes create/list/cancel/status actions for the background scheduler.
The LLM translates natural language ("tous les matins à 8h") into
structured schedule parameters.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from src.skills.base import BaseSkill, validate_input

_scheduler_instance = None


def _get_scheduler():
    """Return the global scheduler instance (set during startup)."""
    return _scheduler_instance


def set_scheduler_instance(scheduler) -> None:
    """Set the global scheduler instance (called during startup)."""
    global _scheduler_instance
    _scheduler_instance = scheduler


class TaskSchedulerSkill(BaseSkill):
    name = "task_scheduler"
    description = (
        "Schedule recurring tasks. Create daily, interval, or cron-based tasks "
        "that run automatically. List, cancel, or check status of scheduled tasks. "
        "Supports result comparison between runs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "list", "cancel", "status"],
                "description": "Action: create, list, cancel, status",
            },
            "label": {
                "type": "string",
                "description": "Human-readable label for the task (e.g. 'Météo Paris 8h')",
            },
            "prompt": {
                "type": "string",
                "description": "What the bot should do when the task triggers (e.g. 'Affiche la météo de Paris')",
            },
            "schedule": {
                "type": "object",
                "description": "Schedule definition",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["daily", "interval", "cron"],
                        "description": "Schedule type: daily (once per day), interval (every N seconds), cron (specific weekdays)",
                    },
                    "hour": {
                        "type": "integer",
                        "description": "Hour (0-23, local time)",
                    },
                    "minute": {"type": "integer", "description": "Minute (0-59)"},
                    "seconds": {
                        "type": "integer",
                        "description": "Interval in seconds (for type=interval)",
                    },
                    "weekdays": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Days of week: Mon=0 to Sun=6 (for type=cron)",
                    },
                },
                "required": ["type"],
            },
            "compare": {
                "type": "boolean",
                "description": "If true, compare with previous result and report changes",
                "default": False,
            },
            "task_id": {
                "type": "string",
                "description": "Task ID (for cancel/status)",
            },
        },
        "required": ["action"],
    }

    @validate_input
    async def execute(
        self,
        workspace_dir: Path,
        action: str = "",
        label: str = "",
        prompt: str = "",
        schedule: dict[str, Any] | None = None,
        compare: bool = False,
        task_id: str = "",
        **kwargs: Any,
    ) -> str:
        scheduler = _get_scheduler()
        if scheduler is None:
            return "❌ Le scheduler n'est pas disponible."

        chat_id = workspace_dir.name

        actions = {
            "create": lambda: self._create(
                scheduler, chat_id, label, prompt, schedule, compare
            ),
            "list": lambda: self._list(scheduler, chat_id),
            "cancel": lambda: self._cancel(scheduler, chat_id, task_id),
            "status": lambda: self._status(scheduler, chat_id, task_id),
        }

        fn = actions.get(action)
        if not fn:
            return f"Action inconnue: {action}. Utilise: {', '.join(actions)}"
        result = fn()
        # Support both sync and async action handlers
        if asyncio.iscoroutine(result):
            return await result
        return result

    async def _create(
        self,
        scheduler,
        chat_id: str,
        label: str,
        prompt: str,
        schedule: dict[str, Any] | None,
        compare: bool,
    ) -> str:
        if not prompt:
            return "❌ Le paramètre 'prompt' est requis (que doit faire le bot ?)"
        if not schedule:
            return "❌ Le paramètre 'schedule' est requis (quand exécuter ?)"

        stype = schedule.get("type", "")
        if stype not in ("daily", "interval", "cron"):
            return (
                f"❌ Type de schedule invalide: {stype}. Utilise: daily, interval, cron"
            )

        if stype in ("daily", "cron") and "hour" not in schedule:
            return "❌ 'hour' est requis pour les types daily et cron"
        if stype == "interval" and "seconds" not in schedule:
            return "❌ 'seconds' est requis pour le type interval"

        task = {
            "label": label or prompt[:50],
            "prompt": prompt,
            "schedule": schedule,
            "compare": compare,
        }
        tid = await scheduler.add_task_async(chat_id, task)
        desc = self._describe_schedule(schedule)
        return f"✅ Tâche planifiée créée !\n- **ID**: {tid}\n- **Label**: {task['label']}\n- **Fréquence**: {desc}\n- **Prompt**: {prompt}"

    def _list(self, scheduler, chat_id: str) -> str:
        tasks = scheduler.list_tasks(chat_id)
        if not tasks:
            return "📋 Aucune tâche planifiée. Crée-en une avec `action: create`."

        lines = ["📋 **Tâches planifiées:**\n"]
        for t in tasks:
            status_icon = "🟢" if t.get("enabled", True) else "🔴"
            desc = self._describe_schedule(t.get("schedule", {}))
            last = t.get("last_run")
            last_str = f"\n   Dernière exécution: {last[:19]}" if last else ""
            compare_str = " (avec comparaison)" if t.get("compare") else ""
            lines.append(
                f"- {status_icon} **{t['task_id']}**: {t.get('label', 'Sans nom')}\n"
                f"   Fréquence: {desc}{compare_str}{last_str}"
            )
        return "\n".join(lines)

    async def _cancel(self, scheduler, chat_id: str, task_id: str) -> str:
        if not task_id:
            return "❌ 'task_id' est requis pour annuler une tâche."
        if await scheduler.remove_task_async(chat_id, task_id):
            return f"✅ Tâche {task_id} supprimée."
        return f"❌ Tâche {task_id} non trouvée."

    def _status(self, scheduler, chat_id: str, task_id: str) -> str:
        if not task_id:
            return self._list(scheduler, chat_id)

        tasks = scheduler.list_tasks(chat_id)
        for t in tasks:
            if t["task_id"] == task_id:
                desc = self._describe_schedule(t.get("schedule", {}))
                lines = [
                    f"📊 **{t['task_id']}: {t.get('label', 'Sans nom')}**\n",
                    f"- Fréquence: {desc}",
                    f"- Prompt: {t.get('prompt', '')}",
                    f"- Comparaison: {'Oui' if t.get('compare') else 'Non'}",
                    f"- Activée: {'Oui' if t.get('enabled', True) else 'Non'}",
                    f"- Créée: {t.get('created', '?')[:19]}",
                    f"- Dernière exécution: {(t.get('last_run') or 'Jamais')[:19]}",
                ]
                if t.get("last_result"):
                    preview = t["last_result"][:300]
                    lines.append(f"- Dernier résultat:\n```\n{preview}\n```")
                return "\n".join(lines)
        return f"❌ Tâche {task_id} non trouvée."

    @staticmethod
    def _describe_schedule(schedule: dict[str, Any]) -> str:
        stype = schedule.get("type", "")
        if stype == "daily":
            h, m = schedule.get("hour", 0), schedule.get("minute", 0)
            return f"Tous les jours à {h:02d}h{m:02d}"
        if stype == "interval":
            secs = schedule.get("seconds", 3600)
            if secs >= 3600:
                hours = secs // 3600
                return f"Toutes les {hours}h"
            if secs >= 60:
                mins = secs // 60
                return f"Toutes les {mins}min"
            return f"Toutes les {secs}s"
        if stype == "cron":
            days = schedule.get("weekdays", [])
            day_names = ["Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim"]
            days_str = ", ".join(day_names[d] for d in days) if days else "Tous"
            h, m = schedule.get("hour", 0), schedule.get("minute", 0)
            return f"{days_str} à {h:02d}h{m:02d}"
        return stype
