<!-- Context: project/concepts/architecture-overview | Priority: high | Version: 2.5 | Updated: 2026-05-04 -->

# Concept: System Architecture Overview

**Core Idea**: Custombot is a Python WhatsApp AI assistant built around a central ReAct loop with a state-machine lifecycle (`AppPhase`). Messages flow from WhatsApp (via neonize) through a middleware pipeline and routing engine into the LLM, which can invoke skills via parallel `TaskGroup` execution, access per-chat memory, vector search, scheduling, and project knowledge — all within isolated per-chat workspaces.

**Source**: `README.md`, `src/app.py`, `src/builder.py`

---

## Key Points

- **State machine lifecycle**: `AppPhase` enum (CREATED → STARTING → RUNNING → SHUTTING_DOWN → STOPPED) with validated transitions and frozen `AppComponents` dataclass
- **Declarative startup**: `StepOrchestrator[T]` executes `ComponentSpec` steps in dependency order with progress bars and timing; `StartupContext.validate_populated()` returns type-safe `_PopulatedStartupContext`
- **Structured DI**: `BotDeps` dataclass replaces 15-param Bot constructor; `ShutdownContext` dataclass for teardown; `BuilderContext` for assembly
- **Middleware pipeline**: Configurable `MessagePipeline` with ordered middleware (dedup, rate-limit, routing, bot processing) — extensible via config
- **Atomic config hot-reload**: `ConfigChangeApplier` swaps entire Config reference atomically; `Bot/LLMProvider/ContextAssembler.update_config()` public methods
- **Immutable turn-preparation**: `_PreparedTurn` frozen dataclass extracted from `_process()` via `_prepare_turn()` — separates turn setup from ReAct orchestration
- **Parallel pre-shutdown**: `config_watcher.stop()` and `workspace_monitor.stop()` run concurrently via `asyncio.gather()`
- **Factory shutdown context**: `AppComponents.to_shutdown_context()` builds `ShutdownContext` from populated state
- **Swap-buffers flush**: `MessageQueue` detaches write buffer under lock, flushes without blocking enqueue
- **Unified dedup**: `DeduplicationService` with in-memory LRU fast-path (10K/5min TTL), fail-open on DB errors, `check_and_record_outbound()` single hash
- **Pre-computed routing**: `MatchingContext` built once in `_build_turn_context()`, reused in `match_with_rule()` cache lookup
- **ReactIterationContext**: Frozen dataclass replaces 18-param threading into `_react_iteration()`
- **Parallel tool execution**: `asyncio.TaskGroup` for concurrent skill calls within ReAct loop, with `MAX_TOOL_CALLS_PER_TURN` guard
- **LLM package**: `src/llm/` with `_client.py`, `_provider.py`, `_error_classifier.py`, `__init__.py` re-exports for backward compatibility
- **Any OpenAI-compatible LLM**: OpenAI, OpenRouter, Ollama, Groq, LM Studio — switch with one config line
- **Native WhatsApp**: neonize (Python ctypes for whatsmeow Go library) — no Node.js bridge
- **Workspace isolation**: Each chat in `.workspace/whatsapp_data/<chat_id>/` — no cross-chat leakage
- **Observable**: OTel tracing spans, structured logging (text/JSON), Prometheus metrics, correlation IDs

---

## Architecture Diagram

```
main.py (Click CLI)
  └─ Application (AppPhase state machine, bounded semaphore, error categorization)
       ├─ StartupOrchestrator → BuilderOrchestrator (StepOrchestrator[T])
       │    └─ BotComponents (frozen dataclass: db, llm, memory, skills, routing, dedup…)
       ├─ MessagePipeline (7-step middleware chain, config-driven, OTel-wrapped)
       │    └─ operation_tracker → metrics → inbound_logging → preflight
       │         → typing → error_handler → handle_message
        ├─ Bot (orchestrator, delegates to submodules)
        │    ├─ preflight.py    (lightweight pre-filter)
        │    ├─ crash_recovery.py (stale message recovery)
        │    └─ react_loop.py   (ReactIterationContext, LLM ↔ tool-call, parallel TaskGroup)
        ├─ LLM Package (src/llm/) — _client.py, _provider.py, _error_classifier.py, __init__.py re-exports
        ├─ EventBus (cross-component pub/sub, 9 events incl. startup_completed, generation_conflict)
       ├─ DeduplicationService (unified: inbound msg-id + outbound xxhash with TTL cache)
       ├─ Channel (neonize WhatsApp / CLI)
        ├─ Scheduler (daily/interval/cron, cached _time_to_next_due, orjson serialization)
        ├─ HealthServer (/health + /metrics, path validation, rate limit, security headers)
        └─ ConfigWatcher + WorkspaceMonitor (background daemons, parallel shutdown)
```

---

## Component Summary

| Component | Module | Purpose |
|-----------|--------|---------|
| Application | `src/app.py` | Lifecycle state machine, startup, shutdown |
| Builder | `src/builder.py` | `StepOrchestrator[T]`-based component assembly |
| Bot | `src/bot/_bot.py` | Thin orchestrator delegating to submodules (`BotDeps` constructor) |
| ReAct Loop | `src/bot/react_loop.py` | LLM ↔ tool-call cycle with retry + streaming |
| Routing | `src/routing.py` | Priority-based rule matching (watchdog + mtime) |
| Middleware | `src/core/message_pipeline.py` | Configurable middleware chain |
| Context | `src/core/context_assembler.py` | Memory + instructions + project context assembly |
| LLM Client | `src/llm/` | OpenAI-compatible async client (chat + stream), provider, error classifier |
| Skills | `src/skills/` | Python + Markdown tools, auto-discovered |
| Memory | `src/memory.py` | Per-chat MEMORY.md + AGENTS.md with mtime caching |
| Vector Memory | `src/vector_memory/` | sqlite-vec semantic search (batch.py, health.py) |
| DB | `src/db/` | SQLite with file pool, JSONL messages, migrations |
| Security | `src/security/` | Prompt injection filter, path validation, audit, signing |
| Monitoring | `src/monitoring/` | OTel tracing, Prometheus metrics, workspace monitor |
| Health | `src/health/` | HTTP `/health`, `/metrics`, `/ready` endpoints |

---

## Related

- `concepts/react-loop.md` — Detailed ReAct pipeline
- `concepts/routing-engine.md` — Message routing mechanics
- `concepts/lifecycle-state-machine.md` — AppPhase + startup flow
- `lookup/workspace-structure.md` — Directory layout
