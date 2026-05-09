"""
src/core/orchestrator.py — Generic base for dependency-ordered step execution.

Provides ``StepOrchestrator[C, S]`` which encapsulates the shared pattern
used by both ``StartupOrchestrator`` and ``BuilderOrchestrator``:

  - Topological sorting of steps via ``_resolve_order()``
  - Per-step execution with logging, timing, and duration tracking via
    ``_execute_step()``
  - Constructor that accepts a context, optional step overrides, and
    a default step registry

Subclasses parameterise by context type ``C`` and spec type ``S``, then
override ``run_all()`` for pre/post-loop customisation (progress bars,
startup banners, result assembly).

Usage::

    class MyOrchestrator(StepOrchestrator[MyContext, MySpec]):
        async def run_all(self) -> SomeResult:
            for spec in self._resolve_order():
                await self._execute_step(spec)
            return self._ctx.build_result()
"""

from __future__ import annotations

import logging
import time
from typing import Any, Generic, Protocol, Sequence, TypeVar

from src.utils.dag import topological_sort
from src.lifecycle import _log_component_init, _log_component_ready

log = logging.getLogger(__name__)


# ── Protocols ─────────────────────────────────────────────────────────────


class _StepContext(Protocol):
    """Minimal protocol for orchestrator context objects.

    Both ``StartupContext`` and ``BuilderContext`` satisfy this protocol
    (they have a ``component_durations: dict[str, float]`` field).
    """

    component_durations: dict[str, float]


class _StepSpec(Protocol):
    """Minimal protocol for step specification objects.

    Both ``ComponentSpec`` and ``BuilderComponentSpec`` satisfy this
    protocol.  The ``factory`` is typed as ``Any`` because each module
    uses its own typed Protocol for the callable.
    """

    name: str
    depends_on: Sequence[str]
    factory: Any  # async (ctx) -> str | None


# ── Generic Orchestrator ──────────────────────────────────────────────────

_C = TypeVar("_C", bound=_StepContext)
_S = TypeVar("_S", bound=_StepSpec)


class StepOrchestrator(Generic[_C, _S]):
    """Execute a sequence of declarative steps in dependency order.

    Handles logging, timing, and error propagation for each step.
    Subclasses provide the concrete context and spec types, plus
    override ``run_all()`` for lifecycle-specific behaviour (e.g.
    progress bars, startup banners, result assembly).
    """

    __slots__ = ("_ctx", "_steps", "_context_label")

    def __init__(
        self,
        ctx: _C,
        steps: Sequence[_S] | None,
        default_steps: Sequence[_S],
        *,
        context_label: str = "dependency",
    ) -> None:
        self._ctx = ctx
        self._steps = list(steps) if steps is not None else list(default_steps)
        self._context_label = context_label

    def _resolve_order(self) -> list[_S]:
        """Topologically sort steps so every step runs after its dependencies.

        When no ``depends_on`` is declared the original list order is
        preserved.  Raises ``ValueError`` on circular or missing deps.
        """
        return topological_sort(
            self._steps,
            key=lambda s: s.name,
            depends_on=lambda s: s.depends_on,
            context_label=self._context_label,
        )

    async def _execute_step(self, spec: _S) -> str | None:
        """Execute a single step with logging, timing, and duration tracking.

        Calls ``spec.factory(self._ctx)``, records elapsed time in
        ``self._ctx.component_durations[spec.name]``, and emits structured
        init/ready log lines via the lifecycle helpers.

        Returns the optional detail string from the factory.
        """
        _log_component_init(spec.name, "started")
        t0 = time.monotonic()
        detail: str | None = await spec.factory(self._ctx)
        elapsed = time.monotonic() - t0
        self._ctx.component_durations[spec.name] = elapsed
        _log_component_ready(spec.name, detail)
        return detail
