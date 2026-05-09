<!-- Context: project/concepts/lifecycle-state-machine | Priority: high | Version: 1.1 | Updated: 2026-05-02 -->

# Concept: Application Lifecycle State Machine

**Core Idea**: The `Application` class in `src/app.py` uses an explicit `AppPhase` enum state machine with validated transitions and a frozen `AppComponents` dataclass to guarantee no partially-initialized state is ever visible to downstream code.

**Source**: `src/app.py`

---

## Key Points

- **AppPhase enum**: `CREATED → STARTING → RUNNING → SHUTTING_DOWN → STOPPED`
- **Validated transitions**: `_transition()` raises `RuntimeError` on illegal phase changes
- **Frozen AppComponents**: `@dataclass(frozen=True)` — all 8 component fields guaranteed non-None after startup
- **Type-safe startup validation**: `StartupContext.validate_populated()` returns `_PopulatedStartupContext` with non-optional types, eliminating `type: ignore` directives
- **Declarative startup**: `StartupOrchestrator` runs `ComponentSpec` steps in order, builds `AppComponents` atomically
- **BuilderOrchestrator**: Same `StepOrchestrator[T]` pattern, executes `BuilderComponentSpec` with dependency resolution
- **Timeout-protected shutdown**: Each cleanup step wrapped in `asyncio.wait_for(timeout=CLEANUP_STEP_TIMEOUT)`

---

## Phase Flow

```
CREATED ──_startup()──▶ STARTING ──(all components ready)──▶ RUNNING
                           │                                   │
                           └──(startup failure)──▶ SHUTTING_DOWN │
                                                        │      │
                                          (signal/ctrl+c)┘      │
                                                        ▼      ▼
                                                     SHUTTING_DOWN
                                                          │
                                                          ▼
                                                       STOPPED
```

---

## Startup Sequence

```
Application._startup()
  └─ StartupOrchestrator(ctx).run_all()
       ├─ Step: Shutdown Manager
       ├─ Step: Bot Components (via BuilderOrchestrator)
       │    ├─ Workspace Integrity
       │    ├─ Database + Dedup
       │    ├─ LLM Client
       │    ├─ Memory
       │    ├─ Vector Memory (depends: DB, LLM)
       │    ├─ Project Store
       │    ├─ Message Queue
       │    ├─ Routing Engine (depends: Project Store)
       │    ├─ Skills Registry (depends: DB, VM, PS, Routing, LLM)
       │    └─ Bot (depends: Skills, MQ, Memory, DB, LLM)
       ├─ Step: Scheduler
       ├─ Step: Channel
       ├─ Step: Message Pipeline
       ├─ Step: Workspace Monitor
       ├─ Step: Config Watcher
       └─ Step: Health Server
```

---

## Quick Example

```python
# Valid transition
app._transition(AppPhase.STARTING)  # CREATED → STARTING ✓

# Invalid transition raises RuntimeError
app._transition(AppPhase.STOPPED)   # CREATED → STOPPED ✗

# Components only accessible after RUNNING
app.state.channel  # raises RuntimeError if phase != RUNNING
```

---

## Codebase

- `src/app.py` — Application class, AppPhase, AppComponents
- `src/builder.py` — BuilderOrchestrator, BuilderComponentSpec
- `src/core/startup.py` — StartupOrchestrator, StartupContext
- `src/core/orchestrator.py` — Generic StepOrchestrator[T] base
- `src/lifecycle.py` — Logging helpers, perform_shutdown()

## Related

- `concepts/architecture-overview.md` — Full system architecture
- `concepts/graceful-shutdown.md` — Ordered component teardown
