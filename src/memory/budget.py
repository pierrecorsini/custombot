"""
memory.budget — Context window budget allocator with configurable slots.

Divides the context window into budgeted categories (system_prompt, tools,
memory, recent_history, current_turn) with configurable percentages.
When a category overflows, redistributes from the most underused category.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

DEFAULT_BUDGET: dict[str, float] = {
    "system_prompt": 0.15,
    "tools": 0.10,
    "memory": 0.20,
    "recent_history": 0.40,
    "current_turn": 0.15,
}


@dataclass(slots=True, frozen=True)
class BudgetAllocation:
    """Token budget per category."""

    system_prompt: int
    tools: int
    memory: int
    recent_history: int
    current_turn: int
    total: int

    def as_dict(self) -> dict[str, int]:
        return {
            "system_prompt": self.system_prompt,
            "tools": self.tools,
            "memory": self.memory,
            "recent_history": self.recent_history,
            "current_turn": self.current_turn,
        }


@dataclass(slots=True)
class _CategoryUsage:
    """Track token usage for a single category."""

    budget: int
    used: int = 0


class ContextBudgetAllocator:
    """Divide the context window into budgeted slots with overflow redistribution."""

    def __init__(
        self,
        total_tokens: int = 100_000,
        budget_config: dict[str, float] | None = None,
    ) -> None:
        self._total = total_tokens
        self._budget = dict(budget_config or DEFAULT_BUDGET)
        self._overrides: dict[str, float] = {}

    def set_override(self, category: str, percentage: float) -> None:
        """Set a custom percentage for a budget category."""
        if category not in DEFAULT_BUDGET:
            log.warning("Unknown budget category: %s", category)
            return
        self._overrides[category] = percentage

    def _effective_budget(self) -> dict[str, float]:
        """Return budget percentages with overrides applied."""
        result = dict(self._budget)
        result.update(self._overrides)
        # Normalize so percentages sum to 1.0
        total = sum(result.values())
        if total <= 0:
            return DEFAULT_BUDGET
        return {k: v / total for k, v in result.items()}

    def allocate(self, total_tokens: int | None = None) -> BudgetAllocation:
        """Compute token allocation for each category."""
        total = total_tokens or self._total
        pct = self._effective_budget()
        return BudgetAllocation(
            system_prompt=int(total * pct.get("system_prompt", 0.15)),
            tools=int(total * pct.get("tools", 0.10)),
            memory=int(total * pct.get("memory", 0.20)),
            recent_history=int(total * pct.get("recent_history", 0.40)),
            current_turn=int(total * pct.get("current_turn", 0.15)),
            total=total,
        )

    def trim_to_budget(
        self,
        messages: list[dict[str, str]],
        allocation: BudgetAllocation,
    ) -> list[dict[str, str]]:
        """Trim messages to fit within their category budgets.

        Categories are mapped to message roles:
          - system_prompt: role="system" (first message)
          - tools: role="system" with tool definitions (heuristic: contains "tool")
          - memory: role="system" with memory content
          - recent_history: role="user" or "assistant"
          - current_turn: last user message

        When a category overflows, excess tokens are redistributed from the
        most underused category.
        """
        if not messages:
            return messages

        from src.llm.token_estimator import estimate_tokens

        # Categorize messages
        system_msgs: list[dict[str, str]] = []
        tool_msgs: list[dict[str, str]] = []
        memory_msgs: list[dict[str, str]] = []
        history_msgs: list[dict[str, str]] = []
        current_turn: list[dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                if '"tool"' in content or '"function"' in content or '"tools"' in content:
                    tool_msgs.append(msg)
                elif "memory" in content.lower() or "MEMORY" in content:
                    memory_msgs.append(msg)
                else:
                    system_msgs.append(msg)
            elif role in ("user", "assistant"):
                history_msgs.append(msg)

        # Separate current turn (last user message)
        if history_msgs and history_msgs[-1].get("role") == "user":
            current_turn = [history_msgs.pop()]

        # Compute token usage per category
        budgets = allocation.as_dict()
        usage: dict[str, _CategoryUsage] = {
            "system_prompt": _CategoryUsage(budget=budgets["system_prompt"]),
            "tools": _CategoryUsage(budget=budgets["tools"]),
            "memory": _CategoryUsage(budget=budgets["memory"]),
            "recent_history": _CategoryUsage(budget=budgets["recent_history"]),
            "current_turn": _CategoryUsage(budget=budgets["current_turn"]),
        }

        # Trim each category to its budget
        trimmed_system = self._trim_list(system_msgs, usage["system_prompt"])
        trimmed_tools = self._trim_list(tool_msgs, usage["tools"])
        trimmed_memory = self._trim_list(memory_msgs, usage["memory"])
        trimmed_history = self._trim_list(history_msgs, usage["recent_history"])
        trimmed_current = self._trim_list(current_turn, usage["current_turn"])

        return trimmed_system + trimmed_tools + trimmed_memory + trimmed_history + trimmed_current

    @staticmethod
    def _trim_list(
        msgs: list[dict[str, str]],
        cat: _CategoryUsage,
    ) -> list[dict[str, str]]:
        """Trim a message list to fit within its budget, dropping oldest first."""
        if not msgs:
            return msgs

        from src.llm.token_estimator import estimate_tokens

        total = sum(estimate_tokens(m.get("content", "")) for m in msgs)
        if total <= cat.budget:
            return msgs

        # Drop from the front (oldest) until within budget
        result = list(msgs)
        while result:
            total -= estimate_tokens(result[0].get("content", ""))
            result.pop(0)
            if total <= cat.budget:
                break
        return result
