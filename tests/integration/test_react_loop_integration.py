"""
test_react_loop_integration.py — Integration test for the full ReAct loop.

Exercises the real ``react_loop()`` function from ``src.bot.react_loop``
with a stub LLM (no mocks on the loop internals), real ``ToolExecutor``
backed by concrete skills, and real ``PerformanceMetrics``.

Verifies end-to-end:
  1. LLM issues tool_calls → tools execute → tool results appended to messages
  2. LLM produces final text response after seeing tool results
  3. ``tool_log`` is correctly assembled with name, args, and result per entry
  4. ``buffered_persist`` contains the right sequence of role/content dicts
  5. The ``messages`` list grows with assistant + tool messages across iterations
  6. Multi-step tool chains work (two sequential tool calls before final response)
  7. Parallel tool calls within a single turn work correctly
"""

from __future__ import annotations

import json
import tempfile
from typing import Any, TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest

from src.bot.react_loop import react_loop
from src.core.tool_formatter import ToolLogEntry
from src.core.tool_executor import ToolExecutor
from src.exceptions import ErrorCode
from src.monitoring import PerformanceMetrics
from src.rate_limiter import RateLimiter
from src.skills import SkillRegistry
from src.skills.base import BaseSkill

from tests.helpers.llm_mocks import make_chat_response, make_tool_call

if TYPE_CHECKING:
    from unittest.mock import MagicMock
    from pathlib import Path


# ── Stub Skills ───────────────────────────────────────────────────────────────


class _CalculatorSkill(BaseSkill):
    """Stub skill that evaluates simple arithmetic expressions."""

    name = "calculator"
    description = "Evaluate a math expression"
    parameters = {
        "type": "object",
        "properties": {
            "expression": {"type": "string", "description": "Math expression to evaluate"},
        },
        "required": ["expression"],
    }

    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        expr = kwargs.get("expression", "0")
        # Only allow safe arithmetic for testing
        result = eval(expr, {"__builtins__": {}}, {})  # noqa: S307
        return f"Result: {result}"


class _LookupSkill(BaseSkill):
    """Stub skill that returns a canned lookup result."""

    name = "lookup"
    description = "Look up information by key"
    parameters = {
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "Key to look up"},
        },
        "required": ["key"],
    }

    _DATA: dict[str, str] = {
        "capital_france": "Paris",
        "population_earth": "~8 billion",
        "python_creator": "Guido van Rossum",
    }

    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        key = kwargs.get("key", "")
        return self._DATA.get(key, f"Unknown key: {key}")


class _FileWriteSkill(BaseSkill):
    """Stub skill that writes content to the workspace, proving workspace isolation."""

    name = "write_note"
    description = "Write a note file to the workspace"
    parameters = {
        "type": "object",
        "properties": {
            "filename": {"type": "string", "description": "File name"},
            "content": {"type": "string", "description": "File content"},
        },
        "required": ["filename", "content"],
    }

    async def execute(self, workspace_dir: Path, **kwargs: Any) -> str:
        filename = kwargs.get("filename", "note.txt")
        content = kwargs.get("content", "")
        path = workspace_dir / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} chars to {filename}"


# ── Helpers ───────────────────────────────────────────────────────────────────


RETRYABLE_CODES = frozenset(
    {
        ErrorCode.LLM_RATE_LIMITED,
        ErrorCode.LLM_TIMEOUT,
        ErrorCode.LLM_CONNECTION_FAILED,
    }
)


def _make_stub_llm(responses: list[MagicMock]) -> AsyncMock:
    """Create a stub LLM that returns the given responses in sequence."""
    llm = AsyncMock()
    llm.chat = AsyncMock(side_effect=responses)
    return llm


def _make_tool_executor(skills: SkillRegistry) -> ToolExecutor:
    """Create a real ToolExecutor backed by the given skill registry."""
    return ToolExecutor(
        skills_registry=skills,
        rate_limiter=RateLimiter(),
        metrics=PerformanceMetrics(),
        audit_log_dir=None,
    )


def _make_skills_registry(*skills: BaseSkill) -> SkillRegistry:
    """Create a SkillRegistry populated with the given skill instances."""
    registry = SkillRegistry()
    for skill in skills:
        registry._skills[skill.name] = skill
    # Expose tool definitions so react_loop can pass them to the LLM
    registry._tool_definitions = [
        {
            "type": "function",
            "function": {
                "name": s.name,
                "description": s.description,
                "parameters": s.parameters,
            },
        }
        for s in skills
    ]
    return registry


def _safe_workspace(tmp_path: Path) -> Path:
    """Create a workspace dir inside tmp_path so path-traversal checks pass."""
    # react_loop's execute_tool_call checks that workspace_dir resolves
    # inside WORKSPACE_DIR.  Patch WORKSPACE_DIR to tmp_path root and
    # return a child workspace.
    return tmp_path / "workspace"


# ── Integration Tests ─────────────────────────────────────────────────────────


class TestReactLoopSingleToolCall:
    """Integration: LLM calls one tool, then produces a final text response."""

    async def test_single_tool_call_end_to_end(self, tmp_path: Path) -> None:
        """
        Stub LLM returns a tool_call for ``calculator``, skill executes,
        then LLM returns a final answer. Verify:
          - tool_log has one entry with correct name/args/result
          - buffered_persist has assistant + tool + final assistant
          - messages list grows through the loop
        """
        skills = _make_skills_registry(_CalculatorSkill())
        workspace = _safe_workspace(tmp_path)
        workspace.mkdir(parents=True, exist_ok=True)

        # LLM response 1: requests calculator tool call
        tc = make_tool_call(
            call_id="call_calc_1",
            name="calculator",
            arguments='{"expression": "2 + 3"}',
        )
        tool_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tc],
        )

        # LLM response 2: final text after seeing tool result
        final_response = make_chat_response(
            content="The answer is 5.",
            finish_reason="stop",
        )

        llm = _make_stub_llm([tool_response, final_response])
        executor = _make_tool_executor(skills)
        metrics = PerformanceMetrics()
        messages: list[dict] = []

        with _patch_workspace_dir(tmp_path):
            text, tool_log, buffered = await react_loop(
                llm=llm,
                metrics=metrics,
                tool_executor=executor,
                chat_id="chat-integration-1",
                messages=messages,
                tools=skills.tool_definitions if skills.tool_definitions else None,
                workspace_dir=workspace,
                max_tool_iterations=5,
                stream_response=False,
                max_retries=1,
                initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )

        # Final text response
        assert text == "The answer is 5."

        # Tool log: one entry for calculator
        assert len(tool_log) == 1
        entry = tool_log[0]
        assert isinstance(entry, ToolLogEntry)
        assert entry.name == "calculator"
        assert entry.args == {"expression": "2 + 3"}
        assert "Result: 5" in entry.result

        # Buffered persist: [assistant (tool_call), tool result]
        assert len(buffered) == 2
        assert buffered[0]["role"] == "assistant"
        assert buffered[1]["role"] == "tool"
        assert "Result: 5" in buffered[1]["content"]

        # Messages list should have grown: assistant msg + tool msg
        assert len(messages) == 2
        assert messages[0]["role"] == "assistant"
        assert messages[1]["role"] == "tool"
        assert messages[1]["tool_call_id"] == "call_calc_1"

        # LLM was called twice: once for tool call, once for final
        assert llm.chat.await_count == 2


class TestReactLoopMultiStepToolChain:
    """Integration: LLM calls two tools sequentially before final response."""

    async def test_two_sequential_tool_calls(self, tmp_path: Path) -> None:
        """
        Stub LLM calls calculator, then lookup, then produces final text.
        Verifies:
          - tool_log has two entries in order
          - buffered_persist has 2 assistant + 2 tool messages
          - messages grows correctly across iterations
        """
        skills = _make_skills_registry(_CalculatorSkill(), _LookupSkill())
        workspace = _safe_workspace(tmp_path)
        workspace.mkdir(parents=True, exist_ok=True)

        # Iteration 1: calculator
        tc1 = make_tool_call(
            call_id="call_calc",
            name="calculator",
            arguments='{"expression": "10 * 4"}',
        )
        response1 = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tc1],
        )

        # Iteration 2: lookup
        tc2 = make_tool_call(
            call_id="call_lookup",
            name="lookup",
            arguments='{"key": "capital_france"}',
        )
        response2 = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tc2],
        )

        # Iteration 3: final answer
        response3 = make_chat_response(
            content="10 * 4 = 40, and the capital of France is Paris.",
            finish_reason="stop",
        )

        llm = _make_stub_llm([response1, response2, response3])
        executor = _make_tool_executor(skills)
        metrics = PerformanceMetrics()
        messages: list[dict] = []

        with _patch_workspace_dir(tmp_path):
            text, tool_log, buffered = await react_loop(
                llm=llm,
                metrics=metrics,
                tool_executor=executor,
                chat_id="chat-chain",
                messages=messages,
                tools=skills.tool_definitions if skills.tool_definitions else None,
                workspace_dir=workspace,
                max_tool_iterations=5,
                stream_response=False,
                max_retries=1,
                initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )

        assert "10 * 4 = 40" in text
        assert "Paris" in text

        # Tool log has 2 entries in order
        assert len(tool_log) == 2
        assert tool_log[0].name == "calculator"
        assert "Result: 40" in tool_log[0].result
        assert tool_log[1].name == "lookup"
        assert tool_log[1].result == "Paris"

        # Buffered persist: [asst1, tool1, asst2, tool2]
        assert len(buffered) == 4
        assert buffered[0]["role"] == "assistant"
        assert buffered[1]["role"] == "tool"
        assert buffered[2]["role"] == "assistant"
        assert buffered[3]["role"] == "tool"

        # Messages: asst1 + tool1 + asst2 + tool2 = 4
        assert len(messages) == 4

        # LLM called 3 times
        assert llm.chat.await_count == 3


class TestReactLoopParallelToolCalls:
    """Integration: LLM requests multiple tools in a single turn (parallel execution)."""

    async def test_two_parallel_tool_calls_in_single_turn(self, tmp_path: Path) -> None:
        """
        Stub LLM returns two tool_calls in one response. Both skills execute
        (via TaskGroup), results are appended in order, then LLM produces
        final text.
        """
        skills = _make_skills_registry(_CalculatorSkill(), _LookupSkill())
        workspace = _safe_workspace(tmp_path)
        workspace.mkdir(parents=True, exist_ok=True)

        # Single response with two parallel tool calls
        tc1 = make_tool_call(
            call_id="call_p1",
            name="calculator",
            arguments='{"expression": "7 * 6"}',
        )
        tc2 = make_tool_call(
            call_id="call_p2",
            name="lookup",
            arguments='{"key": "python_creator"}',
        )
        tool_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tc1, tc2],
        )

        final_response = make_chat_response(
            content="7*6=42 and Python was created by Guido van Rossum.",
            finish_reason="stop",
        )

        llm = _make_stub_llm([tool_response, final_response])
        executor = _make_tool_executor(skills)
        metrics = PerformanceMetrics()
        messages: list[dict] = []

        with _patch_workspace_dir(tmp_path):
            text, tool_log, buffered = await react_loop(
                llm=llm,
                metrics=metrics,
                tool_executor=executor,
                chat_id="chat-parallel",
                messages=messages,
                tools=skills.tool_definitions if skills.tool_definitions else None,
                workspace_dir=workspace,
                max_tool_iterations=5,
                stream_response=False,
                max_retries=1,
                initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )

        assert "42" in text
        assert "Guido" in text

        # Tool log: 2 entries from parallel execution
        assert len(tool_log) == 2
        tool_names = {e.name for e in tool_log}
        assert tool_names == {"calculator", "lookup"}

        # Buffered persist: [assistant (tool_calls), tool1, tool2]
        assert len(buffered) == 3
        assert buffered[0]["role"] == "assistant"
        assert buffered[1]["role"] == "tool"
        assert buffered[2]["role"] == "tool"

        # Messages: assistant + 2 tool results
        assert len(messages) == 3


class TestReactLoopToolLogAssembly:
    """Integration: Verify tool-log entries carry correct structured data."""

    async def test_tool_log_entry_fields_populated(self, tmp_path: Path) -> None:
        """
        Each ToolLogEntry must have:
          - name: the skill's function name
          - args: parsed JSON dict from the LLM's tool_call arguments
          - result: the string returned by skill.execute()
        """
        skills = _make_skills_registry(_FileWriteSkill())
        workspace = _safe_workspace(tmp_path)
        workspace.mkdir(parents=True, exist_ok=True)

        tc = make_tool_call(
            call_id="call_write",
            name="write_note",
            arguments='{"filename": "hello.txt", "content": "Hello World"}',
        )
        tool_response = make_chat_response(
            content=None,
            finish_reason="tool_calls",
            tool_calls=[tc],
        )
        final_response = make_chat_response(
            content="I wrote the note for you.",
            finish_reason="stop",
        )

        llm = _make_stub_llm([tool_response, final_response])
        executor = _make_tool_executor(skills)
        metrics = PerformanceMetrics()
        messages: list[dict] = []

        with _patch_workspace_dir(tmp_path):
            text, tool_log, buffered = await react_loop(
                llm=llm,
                metrics=metrics,
                tool_executor=executor,
                chat_id="chat-log-verify",
                messages=messages,
                tools=skills.tool_definitions if skills.tool_definitions else None,
                workspace_dir=workspace,
                max_tool_iterations=5,
                stream_response=False,
                max_retries=1,
                initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )

        assert text == "I wrote the note for you."
        assert len(tool_log) == 1

        entry = tool_log[0]
        assert entry.name == "write_note"
        assert entry.args == {"filename": "hello.txt", "content": "Hello World"}
        assert "Wrote 11 chars to hello.txt" == entry.result

        # Verify the file was actually written to disk
        written_file = workspace / "hello.txt"
        assert written_file.exists()
        assert written_file.read_text(encoding="utf-8") == "Hello World"


class TestReactLoopMessageBufferIntegrity:
    """Integration: Verify buffered_persist message sequence is correct."""

    async def test_buffered_persist_contains_full_conversation_trace(self, tmp_path: Path) -> None:
        """
        After a 2-step tool chain, buffered_persist should contain:
          [asst(turn1), tool(turn1), asst(turn2), tool(turn2)]

        Each entry has role/content and tool entries also have 'name'.
        """
        skills = _make_skills_registry(_CalculatorSkill(), _LookupSkill())
        workspace = _safe_workspace(tmp_path)
        workspace.mkdir(parents=True, exist_ok=True)

        tc1 = make_tool_call(
            call_id="call_b1",
            name="calculator",
            arguments='{"expression": "1 + 1"}',
        )
        tc2 = make_tool_call(
            call_id="call_b2",
            name="lookup",
            arguments='{"key": "population_earth"}',
        )

        r1 = make_chat_response(content=None, finish_reason="tool_calls", tool_calls=[tc1])
        r2 = make_chat_response(content=None, finish_reason="tool_calls", tool_calls=[tc2])
        r3 = make_chat_response(content="Done!", finish_reason="stop")

        llm = _make_stub_llm([r1, r2, r3])
        executor = _make_tool_executor(skills)
        metrics = PerformanceMetrics()

        with _patch_workspace_dir(tmp_path):
            _, _, buffered = await react_loop(
                llm=llm,
                metrics=metrics,
                tool_executor=executor,
                chat_id="chat-buffer",
                messages=[],
                tools=skills.tool_definitions if skills.tool_definitions else None,
                workspace_dir=workspace,
                max_tool_iterations=5,
                stream_response=False,
                max_retries=1,
                initial_delay=0.01,
                retryable_codes=RETRYABLE_CODES,
            )

        # 4 entries: asst1, tool1, asst2, tool2
        assert len(buffered) == 4

        # Verify roles alternate correctly
        assert buffered[0]["role"] == "assistant"
        assert buffered[1]["role"] == "tool"
        assert buffered[2]["role"] == "assistant"
        assert buffered[3]["role"] == "tool"

        # Tool entries should have 'name' field
        assert buffered[1]["name"] == "calculator"
        assert buffered[3]["name"] == "lookup"

        # Tool content contains actual skill output
        assert "Result: 2" in buffered[1]["content"]
        assert "~8 billion" in buffered[3]["content"]


# ── Patch helper ──────────────────────────────────────────────────────────────

from contextlib import contextmanager


@contextmanager
def _patch_workspace_dir(tmp_path: Path):
    """Patch WORKSPACE_DIR in react_loop to tmp_path so path-traversal passes."""
    import src.bot.react_loop as _mod

    original = _mod.WORKSPACE_DIR
    _mod.WORKSPACE_DIR = str(tmp_path)
    try:
        yield
    finally:
        _mod.WORKSPACE_DIR = original
