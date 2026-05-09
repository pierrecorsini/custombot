"""
bot.plan_execute — Plan-and-execute agent pattern.

For complex tasks, adds a planning step that generates a task breakdown,
then executes steps sequentially with verification.  An alternative
strategy to the ReAct loop for multi-step tasks that benefit from
upfront planning.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.exceptions import LLMError

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletionMessageParam,
        ChatCompletionToolParam,
    )
    from src.core.tool_executor import ToolExecutor
    from src.llm._provider import LLMProvider
    from src.monitoring import PerformanceMetrics
    from pathlib import Path

log = logging.getLogger(__name__)

_PLAN_SYSTEM = (
    "You are a task planner. Given a user request, produce a JSON array "
    "of subtask objects with 'description' and 'expected_outcome' fields. "
    "Return ONLY the JSON array, no markdown fences or commentary. "
    "Each subtask should be a concrete, verifiable step. "
    "Maximum 10 subtasks."
)

_EXECUTE_SYSTEM = (
    "You are a task executor. Execute the current subtask using the "
    "available tools when needed. After each subtask, report your result "
    "concisely. If a subtask fails, say so clearly so the planner can "
    "adjust. Be thorough but efficient."
)

_VERIFY_PROMPT = (
    "Given the subtask description, expected outcome, and actual result "
    "below, answer ONLY 'PASS' or 'FAIL: <reason>'.\n\n"
    "Subtask: {description}\n"
    "Expected: {expected}\n"
    "Actual: {result}"
)

_DEFAULT_MAX_STEPS = 10


@dataclass(slots=True, frozen=True)
class PlanStep:
    """A single subtask in the execution plan."""

    index: int
    description: str
    expected_outcome: str


@dataclass(slots=True)
class PlanResult:
    """Outcome of a plan-and-execute run."""

    response: str
    steps_completed: int
    steps_total: int
    steps_passed: int


class PlanExecuteAgent:
    """Plan-then-execute strategy for complex multi-step tasks.

    Step 1 — Planning: LLM generates a list of subtasks.
    Step 2 — Execution: subtasks are executed one by one.
    Step 3 — Verification: after each step, the result is checked.
    If a step fails, the planner can replan from that point.
    """

    def __init__(
        self,
        llm: LLMProvider,
        tool_executor: ToolExecutor,
        *,
        max_steps: int = _DEFAULT_MAX_STEPS,
        enabled: bool = True,
    ) -> None:
        self._llm = llm
        self._tool_executor = tool_executor
        self._max_steps = max_steps
        self.enabled = enabled

    async def run(
        self,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        workspace_dir: Path,
        chat_id: str,
    ) -> PlanResult:
        """Execute the plan-and-execute cycle.

        Returns a ``PlanResult`` with the final response and execution
        statistics.
        """
        plan = await self._generate_plan(messages)
        if not plan:
            # Fallback to normal LLM call if planning yields nothing.
            response = await self._llm.chat(messages, tools=tools, chat_id=chat_id)
            text = response.choices[0].message.content or ""
            return PlanResult(
                response=text,
                steps_completed=0,
                steps_total=0,
                steps_passed=0,
            )

        log.info(
            "Plan generated: %d steps for chat %s",
            len(plan),
            chat_id,
        )

        results: list[str] = []
        passed = 0
        completed = 0

        for step in plan[:self._max_steps]:
            result = await self._execute_step(
                step, messages, tools, workspace_dir, chat_id
            )
            completed += 1

            verified = await self._verify_step(step, result)
            if verified:
                passed += 1
                results.append(f"✅ Step {step.index + 1}: {step.description}\n{result}")
            else:
                results.append(f"❌ Step {step.index + 1}: {step.description}\n{result}")
                # Replan from failure point
                replan = await self._replan_from(messages, step, result, plan[completed:])
                if replan:
                    plan = plan[:completed] + replan
                    log.info("Replanned: %d total steps for chat %s", len(plan), chat_id)

        summary = "\n\n".join(results)
        return PlanResult(
            response=summary,
            steps_completed=completed,
            steps_total=len(plan),
            steps_passed=passed,
        )

    # ── internals ────────────────────────────────────────────────────────

    async def _generate_plan(
        self,
        messages: list[ChatCompletionMessageParam],
    ) -> list[PlanStep]:
        """Ask the LLM to produce a plan as a JSON array."""
        user_text = ""
        for m in reversed(messages):
            content = m.get("content", "")
            if m.get("role") == "user" and isinstance(content, str) and content.strip():
                user_text = content
                break

        if not user_text:
            return []

        plan_messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": user_text},
        ]

        try:
            response = await self._llm.chat(plan_messages)
            content = response.choices[0].message.content or "[]"
        except LLMError:
            log.warning("Plan generation failed", exc_info=True)
            return []

        return _parse_plan(content)

    async def _execute_step(
        self,
        step: PlanStep,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        workspace_dir: Path,
        chat_id: str,
    ) -> str:
        """Execute a single plan step."""
        exec_messages = list(messages)
        exec_messages.append(
            {"role": "system", "content": _EXECUTE_SYSTEM},
        )
        exec_messages.append(
            {"role": "user", "content": f"Execute this subtask: {step.description}"},
        )

        try:
            response = await self._llm.chat(exec_messages, tools=tools, chat_id=chat_id)
            return response.choices[0].message.content or "(no result)"
        except LLMError as exc:
            return f"Error: {exc.message}"

    async def _verify_step(self, step: PlanStep, result: str) -> bool:
        """Ask the LLM whether the step outcome meets expectations."""
        verify_messages: list[ChatCompletionMessageParam] = [
            {"role": "user", "content": _VERIFY_PROMPT.format(
                description=step.description,
                expected=step.expected_outcome,
                result=result[:1000],
            )},
        ]

        try:
            response = await self._llm.chat(verify_messages)
            content = (response.choices[0].message.content or "").strip().upper()
            return content.startswith("PASS")
        except LLMError:
            # If verification fails, assume pass to avoid blocking.
            return True

    async def _replan_from(
        self,
        messages: list[ChatCompletionMessageParam],
        failed_step: PlanStep,
        failed_result: str,
        remaining: list[PlanStep],
    ) -> list[PlanStep]:
        """Ask the LLM to replan from the failed step."""
        remaining_desc = ", ".join(s.description for s in remaining)
        replan_messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": (
                f"The step '{failed_step.description}' failed with result: "
                f"{failed_result[:500]}\n\n"
                f"Remaining steps were: {remaining_desc}\n\n"
                "Create a revised plan for the remaining work."
            )},
        ]

        try:
            response = await self._llm.chat(replan_messages)
            content = response.choices[0].message.content or "[]"
            new_steps = _parse_plan(content)
            # Re-index from the failed step position
            return [
                PlanStep(index=failed_step.index + i + 1, description=s.description, expected_outcome=s.expected_outcome)
                for i, s in enumerate(new_steps)
            ]
        except LLMError:
            return remaining


def _parse_plan(content: str) -> list[PlanStep]:
    """Parse LLM output into a list of PlanStep objects."""
    # Strip markdown fences if present.
    text = content.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1])

    try:
        items = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        log.debug("Plan parse failed, treating as single step")
        if text:
            return [PlanStep(index=0, description=text, expected_outcome="completed")]
        return []

    if not isinstance(items, list):
        return []

    steps: list[PlanStep] = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            steps.append(PlanStep(
                index=i,
                description=item.get("description", f"Step {i + 1}"),
                expected_outcome=item.get("expected_outcome", "completed"),
            ))
    return steps
