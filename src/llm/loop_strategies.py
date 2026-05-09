"""
llm.loop_strategies — Configurable ReAct loop strategies.

Provides different strategies for the LLM reasoning loop:
  - StandardReAct: Current tool-call loop behaviour.
  - ChainOfThought: Adds an explicit thinking step before acting.
  - Reflexion: Self-correcting with retry on low-quality responses.
  - TreeOfThought: Multi-path exploration for complex tasks.

Strategy selection happens via config key ``react_loop_strategy``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from openai.types.chat import (
        ChatCompletion,
        ChatCompletionMessageParam,
        ChatCompletionToolParam,
    )
    from src.llm import LLMProvider

log = logging.getLogger(__name__)


@runtime_checkable
class LoopStrategy(Protocol):
    """Protocol for ReAct loop strategies."""

    async def execute(
        self,
        llm: LLMProvider,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        *,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> ChatCompletion: ...


class StandardReAct:
    """Standard ReAct: call LLM, process tool calls, loop until stop.

    This is the current default behaviour — no modifications to the
    existing loop.
    """

    async def execute(
        self,
        llm: LLMProvider,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        *,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> ChatCompletion:
        return await llm.chat(messages, tools=tools, chat_id=chat_id, timeout=timeout)


class ChainOfThought:
    """Chain-of-thought: inject a 'think step' message before each LLM call.

    Prepends a system-level nudge encouraging step-by-step reasoning
    before the LLM responds.  Useful for complex reasoning tasks.
    """

    THINK_NUDGE: ChatCompletionMessageParam = {
        "role": "system",
        "content": (
            "Before responding, think through the problem step by step. "
            "Consider what information you have, what you need, and the "
            "best approach before acting."
        ),
    }

    async def execute(
        self,
        llm: LLMProvider,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        *,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> ChatCompletion:
        # Only inject once — check if nudge is already present
        nudge_text = self.THINK_NUDGE.get("content", "")
        has_nudge = any(
            m.get("role") == "system" and m.get("content") == nudge_text
            for m in messages
        )
        augmented = messages if has_nudge else [self.THINK_NUDGE, *messages]
        return await llm.chat(augmented, tools=tools, chat_id=chat_id, timeout=timeout)


class Reflexion:
    """Reflexion: self-correcting strategy with quality retry.

    After the LLM responds, evaluates quality and retries with
    self-critique if the response is unsatisfactory.  Max one retry.
    """

    MAX_REFLECTION_RETRIES = 1

    async def execute(
        self,
        llm: LLMProvider,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        *,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> ChatCompletion:
        completion = await llm.chat(messages, tools=tools, chat_id=chat_id, timeout=timeout)

        # Only reflect on terminal responses (no tool calls)
        if not _is_terminal_response(completion):
            return completion

        response_text = _extract_response_text(completion)
        if not response_text or len(response_text) < 50:
            return completion

        # Ask for self-critique
        critique = await _get_self_critique(
            llm, messages, response_text, chat_id=chat_id
        )
        if not critique:
            return completion

        # Check if the critique indicates improvement needed
        if _needs_improvement(critique):
            log.info("Reflexion: retrying with self-critique for chat %s", chat_id)
            retry_messages = list(messages)
            retry_messages.append(
                {"role": "assistant", "content": response_text}
            )
            retry_messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Self-critique: {critique}\n\n"
                        "Please improve your previous response based on this critique."
                    ),
                }
            )
            return await llm.chat(
                retry_messages, tools=tools, chat_id=chat_id, timeout=timeout
            )

        return completion


class TreeOfThought:
    """Tree-of-thought: explore multiple reasoning paths.

    Generates multiple candidate responses and selects the best one.
    Increases latency but improves quality on complex problems.
    """

    NUM_PATHS = 3

    async def execute(
        self,
        llm: LLMProvider,
        messages: list[ChatCompletionMessageParam],
        tools: list[ChatCompletionToolParam] | None,
        *,
        chat_id: str | None = None,
        timeout: float | None = None,
    ) -> ChatCompletion:
        import asyncio

        first_response = await llm.chat(
            messages, tools=tools, chat_id=chat_id, timeout=timeout
        )

        # Only branch on terminal responses
        if not _is_terminal_response(first_response):
            return first_response

        response_text = _extract_response_text(first_response)
        if not response_text:
            return first_response

        # Generate alternative paths with temperature variation
        async def _generate_alt() -> ChatCompletion:
            return await llm.chat(messages, tools=tools, chat_id=chat_id, timeout=timeout)

        try:
            alternatives = await asyncio.gather(
                *[_generate_alt() for _ in range(self.NUM_PATHS - 1)],
                return_exceptions=True,
            )
        except Exception:
            return first_response

        candidates = [response_text]
        for alt in alternatives:
            if isinstance(alt, BaseException):
                continue
            alt_text = _extract_response_text(alt)
            if alt_text:
                candidates.append(alt_text)

        if len(candidates) <= 1:
            return first_response

        # Select the best candidate via the LLM
        best = await _select_best_candidate(
            llm, candidates, chat_id=chat_id
        )

        # Return the original completion shape with the best text
        if best and best != response_text:
            _patch_response_text(first_response, best)

        return first_response


# ── Strategy registry ────────────────────────────────────────────────────

STRATEGIES: dict[str, type[LoopStrategy]] = {
    "standard": StandardReAct,
    "chain_of_thought": ChainOfThought,
    "reflexion": Reflexion,
    "tree_of_thought": TreeOfThought,
}


def get_strategy(name: str) -> LoopStrategy:
    """Return a strategy instance by name (case-insensitive).

    Falls back to ``StandardReAct`` for unknown names.
    """
    cls = STRATEGIES.get(name.lower())
    if cls is None:
        log.warning("Unknown loop strategy %r — falling back to 'standard'", name)
        cls = StandardReAct
    return cls()


# ── Helpers ──────────────────────────────────────────────────────────────


def _is_terminal_response(completion: ChatCompletion) -> bool:
    """True if the response has no tool calls (terminal)."""
    if not completion.choices:
        return True
    msg = completion.choices[0].message
    return not msg.tool_calls


def _extract_response_text(completion: ChatCompletion) -> str:
    """Extract text content from a ChatCompletion."""
    if not completion.choices:
        return ""
    content = completion.choices[0].message.content
    return content or ""


def _patch_response_text(completion: ChatCompletion, text: str) -> None:
    """Replace the text in a ChatCompletion in-place."""
    if completion.choices:
        completion.choices[0].message.content = text


async def _get_self_critique(
    llm: LLMProvider,
    original_messages: list[ChatCompletionMessageParam],
    response: str,
    *,
    chat_id: str | None = None,
) -> str | None:
    """Ask the LLM to critique its own response."""
    critique_messages: list[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": (
                "You are a quality evaluator. Critique the following response "
                "concisely. If the response is good, say 'GOOD'. "
                "If it needs improvement, explain why briefly."
            ),
        },
        {"role": "user", "content": response[:2000]},
    ]
    try:
        result = await llm.chat(critique_messages, chat_id=chat_id)
        return _extract_response_text(result)
    except Exception:
        return None


async def _select_best_candidate(
    llm: LLMProvider,
    candidates: list[str],
    *,
    chat_id: str | None = None,
) -> str | None:
    """Ask the LLM to select the best candidate from alternatives."""
    numbered = "\n\n".join(
        f"--- Option {i + 1} ---\n{c[:1000]}" for i, c in enumerate(candidates)
    )
    judge_messages: list[ChatCompletionMessageParam] = [
        {
            "role": "system",
            "content": (
                "Select the best response option. Reply with ONLY the "
                "option number (e.g., '1', '2', or '3')."
            ),
        },
        {"role": "user", "content": numbered},
    ]
    try:
        result = await llm.chat(judge_messages, chat_id=chat_id)
        text = _extract_response_text(result).strip()
        for ch in text:
            if ch.isdigit():
                idx = int(ch) - 1
                if 0 <= idx < len(candidates):
                    return candidates[idx]
        return candidates[0]
    except Exception:
        return candidates[0]


def _needs_improvement(critique: str) -> bool:
    """Check if the critique indicates the response needs improvement."""
    return critique.strip().upper() != "GOOD"
