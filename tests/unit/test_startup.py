"""
Tests for src/core/startup.py — StartupOrchestrator dependency resolution.

Covers:
- _resolve_order() preserves list order when no depends_on declared
- _resolve_order() topologically sorts based on depends_on
- _resolve_order() raises on circular dependencies
- _resolve_order() raises on unknown dependency names
- run_all() executes steps in dependency-resolved order
- DEFAULT_STARTUP_STEPS resolves without error
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.startup import (
    ComponentSpec,
    DEFAULT_STARTUP_STEPS,
    StartupContext,
    StartupOrchestrator,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_ctx() -> StartupContext:
    """Build a minimal StartupContext with mocked internals."""
    return StartupContext(
        config=MagicMock(),
        session_metrics=MagicMock(),
        app=MagicMock(),
    )


async def _noop_step(ctx: StartupContext) -> str | None:
    return None


# ── _resolve_order() ────────────────────────────────────────────────────


class TestResolveOrder:
    """Tests for StartupOrchestrator._resolve_order()."""

    def test_no_deps_preserves_list_order(self) -> None:
        """When no depends_on is declared, original list order is kept."""
        steps = [
            ComponentSpec(name="A", factory=_noop_step),
            ComponentSpec(name="B", factory=_noop_step),
            ComponentSpec(name="C", factory=_noop_step),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        resolved = orch._resolve_order()
        assert [s.name for s in resolved] == ["A", "B", "C"]

    def test_linear_chain_resolved_correctly(self) -> None:
        """A → B → C dependency chain resolves to A, B, C."""
        steps = [
            ComponentSpec(name="C", factory=_noop_step, depends_on=("B",)),
            ComponentSpec(name="A", factory=_noop_step),
            ComponentSpec(name="B", factory=_noop_step, depends_on=("A",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        resolved = orch._resolve_order()
        names = [s.name for s in resolved]
        assert names.index("A") < names.index("B") < names.index("C")

    def test_diamond_dependency(self) -> None:
        """Diamond: A → B, A → C, B+C → D. All deps satisfied."""
        steps = [
            ComponentSpec(name="D", factory=_noop_step, depends_on=("B", "C")),
            ComponentSpec(name="B", factory=_noop_step, depends_on=("A",)),
            ComponentSpec(name="A", factory=_noop_step),
            ComponentSpec(name="C", factory=_noop_step, depends_on=("A",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        resolved = orch._resolve_order()
        names = [s.name for s in resolved]
        assert names.index("A") < names.index("B")
        assert names.index("A") < names.index("C")
        assert names.index("B") < names.index("D")
        assert names.index("C") < names.index("D")

    def test_circular_dependency_raises(self) -> None:
        """Circular deps raise ValueError."""
        steps = [
            ComponentSpec(name="A", factory=_noop_step, depends_on=("B",)),
            ComponentSpec(name="B", factory=_noop_step, depends_on=("A",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        with pytest.raises(ValueError, match="Circular startup dependency"):
            orch._resolve_order()

    def test_circular_error_message_contains_node_name(self) -> None:
        """Circular dependency error message includes the problematic node."""
        steps = [
            ComponentSpec(name="alpha", factory=_noop_step, depends_on=("beta",)),
            ComponentSpec(name="beta", factory=_noop_step, depends_on=("alpha",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        with pytest.raises(ValueError) as exc_info:
            orch._resolve_order()
        msg = str(exc_info.value)
        assert "alpha" in msg or "beta" in msg

    def test_three_node_circular_dependency(self) -> None:
        """Three-node cycle A→B→C→A raises ValueError."""
        steps = [
            ComponentSpec(name="A", factory=_noop_step, depends_on=("C",)),
            ComponentSpec(name="B", factory=_noop_step, depends_on=("A",)),
            ComponentSpec(name="C", factory=_noop_step, depends_on=("B",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        with pytest.raises(ValueError, match="Circular startup dependency"):
            orch._resolve_order()

    def test_self_referential_dependency(self) -> None:
        """Step depending on itself raises circular dependency."""
        steps = [
            ComponentSpec(name="A", factory=_noop_step, depends_on=("A",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        with pytest.raises(ValueError, match="Circular startup dependency"):
            orch._resolve_order()

    def test_unknown_dependency_raises(self) -> None:
        """Referencing a non-existent step raises ValueError."""
        steps = [
            ComponentSpec(name="A", factory=_noop_step, depends_on=("Missing",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        with pytest.raises(ValueError, match="Unknown startup dependency"):
            orch._resolve_order()

    def test_unknown_dependency_error_message_contains_name(self) -> None:
        """Missing dependency error includes the unknown dep name."""
        steps = [
            ComponentSpec(name="A", factory=_noop_step, depends_on=("nonexistent",)),
        ]
        orch = StartupOrchestrator(_make_ctx(), steps=steps)
        with pytest.raises(ValueError) as exc_info:
            orch._resolve_order()
        assert "nonexistent" in str(exc_info.value)

    def test_empty_steps_list(self) -> None:
        """Empty step list resolves to empty."""
        orch = StartupOrchestrator(_make_ctx(), steps=[])
        assert orch._resolve_order() == []


# ── run_all() execution order ────────────────────────────────────────────


class TestRunAllOrder:
    """Tests for StartupOrchestrator.run_all() execution order."""

    @pytest.mark.asyncio
    async def test_steps_execute_in_dependency_order(self) -> None:
        """run_all() executes steps after their dependencies."""
        execution_order: list[str] = []

        async def _track(name: str):
            async def _step(ctx: StartupContext) -> str | None:
                execution_order.append(name)
                return None

            return _step

        steps = [
            ComponentSpec(name="C", factory=await _track("C"), depends_on=("B",)),
            ComponentSpec(name="A", factory=await _track("A")),
            ComponentSpec(name="B", factory=await _track("B"), depends_on=("A",)),
        ]
        ctx = _make_ctx()
        orch = StartupOrchestrator(ctx, steps=steps)
        await orch.run_all()

        assert execution_order.index("A") < execution_order.index("B")
        assert execution_order.index("B") < execution_order.index("C")


# ── DEFAULT_STARTUP_STEPS validity ──────────────────────────────────────


class TestDefaultSteps:
    """Verify the built-in startup step registry is well-formed."""

    def test_default_steps_resolve_without_error(self) -> None:
        """DEFAULT_STARTUP_STEPS has no circular or missing deps."""
        orch = StartupOrchestrator(_make_ctx())
        resolved = orch._resolve_order()
        assert len(resolved) == len(DEFAULT_STARTUP_STEPS)

    def test_all_dep_names_exist_in_registry(self) -> None:
        """Every depends_on name references a step that exists."""
        names = {s.name for s in DEFAULT_STARTUP_STEPS}
        for spec in DEFAULT_STARTUP_STEPS:
            for dep in spec.depends_on:
                assert dep in names, f"{spec.name} depends on unknown step '{dep}'"

    def test_default_order_matches_resolved_order(self) -> None:
        """The manually-ordered DEFAULT_STARTUP_STEPS is a valid topological order."""
        orch = StartupOrchestrator(_make_ctx())
        resolved = orch._resolve_order()
        original_names = [s.name for s in DEFAULT_STARTUP_STEPS]
        resolved_names = [s.name for s in resolved]
        # The resolved order should produce the same sequence since the
        # manual list was already topologically sorted.
        assert resolved_names == original_names
