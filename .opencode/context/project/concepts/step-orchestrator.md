<!-- Context: project/concepts/step-orchestrator | Priority: medium | Version: 1.1 | Updated: 2026-05-02 -->

# Concept: Step Orchestrator Pattern

**Core Idea**: A generic `StepOrchestrator[C, S]` base class that executes declarative step specifications in dependency-resolved (topological) order with per-step logging, timing, and duration tracking. Used by both `StartupOrchestrator` (app startup) and `BuilderOrchestrator` (bot component assembly).

**Source**: `src/core/orchestrator.py`

---

## Key Points

- **Generic**: Parameterized by context type `C` and spec type `S`
- **Topological sort**: `depends_on` fields are resolved via `src/utils/dag.py` — steps run after their dependencies
- **Per-step timing**: Each step's elapsed time is recorded in `ctx.component_durations[step_name]`
- **Structured logging**: Init/ready log lines via lifecycle helpers
- **Declarative specs**: Each step is a `(name, factory, depends_on)` tuple
- **Error propagation**: Step failures propagate to the caller — no silent swallowing

---

## Architecture

```
StepOrchestrator[C, S]  (generic base)
  ├─ StartupOrchestrator (C=StartupContext, S=ComponentSpec)
  │    └─ 11 startup steps (shutdown_mgr → thread_pool → bot → scheduler → channel → ...)
  └─ BuilderOrchestrator (C=BuilderContext, S=BuilderComponentSpec)
       └─ 10 builder steps (workspace_integrity → sqlite_pool → database → llm → ...)
```

---

## Step Spec Protocol

```python
class _StepSpec(Protocol):
    name: str            # Human-readable name
    depends_on: Sequence[str]  # Names of prerequisite steps
    factory: Any         # async (ctx) -> str | None
```

---

## Execution Flow

```
orchestrator.run_all()
  └─ _resolve_order()  → topological_sort(steps)
  └─ for each step:
       ├─ _log_component_init(step.name, "started")
       ├─ t0 = time.monotonic()
       ├─ detail = await step.factory(ctx)
       ├─ ctx.component_durations[step.name] = elapsed
       └─ _log_component_ready(step.name, detail)
```

---

## Codebase

- `src/core/orchestrator.py` — StepOrchestrator[C, S] generic base
- `src/core/startup.py` — StartupOrchestrator, ComponentSpec, StartupContext, `_PopulatedStartupContext`, 11 steps
- `src/builder.py` — BuilderOrchestrator, BuilderComponentSpec, BuilderContext, 10 steps
- `src/utils/dag.py` — topological_sort() for dependency resolution
- `src/lifecycle.py` — _log_component_init(), _log_component_ready()

## Related

- `concepts/lifecycle-state-machine.md` — How StartupOrchestrator drives the AppPhase state machine
- `concepts/architecture-overview.md` — How builders assemble components
