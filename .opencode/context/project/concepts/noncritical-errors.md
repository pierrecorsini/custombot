<!-- Context: project/concepts/noncritical-errors | Priority: medium | Version: 1.0 | Updated: 2026-05-02 -->

# Concept: Non-Critical Error System

**Core Idea**: A structured, categorized error logging system for fire-and-forget operations where failure must never break the caller's control flow. Each non-critical failure is tagged with a `non_critical` extra field for structured log filtering. Used across 20+ call sites in the codebase.

**Source**: `src/core/errors.py`

---

## Key Points

- **Categorized**: 25+ `NonCriticalCategory` enum values (metrics, cleanup, embedding, streaming, etc.)
- **Structured logging**: Every call adds `non_critical: <category>` to the `extra` dict for log aggregation
- **Fire-and-forget**: Errors are logged at DEBUG level by default with `exc_info=True`, never propagated
- **Consistent pattern**: All "best-effort" operations use `log_noncritical()` instead of bare `try/except: pass`
- **Observable**: Filterable via structured logging (`non_critical` field in JSON logs)

---

## Categories

| Category | When Used |
|----------|-----------|
| `EVENT_EMISSION` | Event bus handler failures |
| `METRICS` | Metrics collection failures |
| `COMPRESSION` | Data compression failures |
| `CLEANUP` | Resource cleanup during shutdown |
| `EMBEDDING` | Embedding model calls |
| `HEALTH_CHECK` | Health server probe failures |
| `SKILL_DISCOVERY` | Skill loading/parsing |
| `STREAMING` | SSE streaming errors |
| `MIDDLEWARE_LOADING` | Custom middleware import failures |
| `CONFIG_LOAD` | Configuration resolution |
| `SKILL_EXECUTION` | Skill runtime errors |
| `SHUTDOWN` | Shutdown cleanup failures |
| + 13 more | Various subsystems |

---

## Usage Pattern

```python
from src.core.errors import NonCriticalCategory, log_noncritical

try:
    get_metrics_collector().track_cache_hit()
except Exception:
    log_noncritical(
        NonCriticalCategory.METRICS,
        "Failed to track cache hit",
        logger=log,
    )
```

---

## Codebase

- `src/core/errors.py` — NonCriticalCategory enum, log_noncritical() function
- `src/app.py` — Event emission failures
- `src/core/message_pipeline.py` — Middleware loading failures
- `src/core/event_bus.py` — Handler invocation failures
- `src/builder.py` — VectorMemory cleanup during startup probe
- `src/lifecycle.py` — Shutdown cleanup failures

## Related

- `concepts/event-bus.md` — Uses non-critical logging for handler failures
- `concepts/graceful-shutdown.md` — Uses non-critical logging for cleanup steps
