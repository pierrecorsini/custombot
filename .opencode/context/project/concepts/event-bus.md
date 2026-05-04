<!-- Context: project/concepts/event-bus | Priority: medium | Version: 1.2 | Updated: 2026-05-04 -->

# Concept: Event Bus

**Core Idea**: A lightweight, async-first, typed event bus (`EventBus`) for cross-component decoupling. Components emit events; subscribers handle them independently — neither side needs to know about the other. Singleton-scoped via `get_event_bus()`.

**Source**: `src/core/event_bus.py`

---

## Key Points

- **Async-first**: All handlers are `async def` coroutines, executed via `asyncio.gather`
- **Error-isolated**: A failing handler is caught, logged via `log_noncritical`, and does not affect other handlers or the emitter
- **Frozen events**: `Event` dataclass is immutable (frozen, slots) — safe for concurrent reads
- **Singleton**: `get_event_bus()` returns global instance; `reset_event_bus()` for testing
- **Graceful close**: `close()` prevents new emissions and clears subscriptions
- **Introspection**: `handler_count()`, `event_names()`, `get_metrics()` for monitoring
- **Max handlers**: 50 per event by default, prevents handler leaks

---

## Built-in Events (10)

| Constant | Name | Emitter |
|----------|------|---------|
| `EVENT_MESSAGE_RECEIVED` | `message_received` | Channel |
| `EVENT_SKILL_EXECUTED` | `skill_executed` | ToolExecutor |
| `EVENT_RESPONSE_SENT` | `response_sent` | HandleMessageMiddleware |
| `EVENT_ERROR_OCCURRED` | `error_occurred` | Application._on_message, Bot._deliver_response |
| `EVENT_SHUTDOWN_STARTED` | `shutdown_started` | GracefulShutdown |
| `EVENT_SCHEDULED_TASK_STARTED` | `scheduled_task_started` | Scheduler |
| `EVENT_SCHEDULED_TASK_COMPLETED` | `scheduled_task_completed` | Scheduler |
| `EVENT_MESSAGE_DROPPED` | `message_dropped` | Bot._build_turn_context |
| `EVENT_GENERATION_CONFLICT` | `generation_conflict` | Bot._deliver_response |
| `EVENT_STARTUP_COMPLETED` | `startup_completed` | Application.run |

---

## Usage

```python
from src.core.event_bus import get_event_bus, Event

bus = get_event_bus()

# Subscribe
async def on_skill(event: Event) -> None:
    log.info("Skill %s in %.1fms", event.data["skill_name"], event.data["duration_ms"])

bus.on("skill_executed", on_skill)

# Emit
await bus.emit(Event(name="skill_executed", data={"skill_name": "bash", "duration_ms": 120.5}, source="ToolExecutor"))

# Unsubscribe
bus.off("skill_executed", on_skill)
```

---

## Codebase

- `src/core/event_bus.py` — EventBus, Event, event constants, singleton accessors
- `src/app.py` — Emits `error_occurred` on pipeline failure, `startup_completed` after startup
- `src/core/tool_executor.py` — Emits `skill_executed` after each tool call
- `src/utils/singleton.py` — `get_or_create_singleton()` backing the singleton pattern
- `src/utils/locking.py` — `AsyncLock` for lazy lock initialization

## Related

- `concepts/middleware-pipeline.md` — Where error events originate
- `concepts/monitoring-metrics.md` — Metrics collected from events
