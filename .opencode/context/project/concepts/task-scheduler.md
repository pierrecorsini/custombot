<!-- Context: project/concepts/task-scheduler | Priority: medium | Version: 1.1 | Updated: 2026-05-04 -->

# Concept: Task Scheduler

**Core Idea**: Background async scheduler that triggers LLM actions on a configurable schedule (daily, interval, cron). Each task builds a prompt, calls the LLM, optionally compares results with previous runs, and delivers the response to WhatsApp. Uses cached datetime parsing and `orjson` for efficient serialization.

**Source**: `src/scheduler.py`

---

## Key Points

- **Three schedule types**: `daily` (hour + minute), `interval` (seconds), `cron` (hour + minute + weekdays)
- **Compare mode**: When enabled, injects previous run result and asks LLM to highlight changes — ideal for monitoring
- **Persistence**: Per-chat storage in `workspace/<chat_id>/.scheduler/tasks.json`
- **Background tick**: Polls every 30s, evaluates each task's `_is_due()` condition
- **Cached datetime**: Parsed `last_run` stored as `task["_last_run_dt"]` — eliminates ~144K string-to-datetime parses/day
- **orjson serialization**: `json_dumps()` uses orjson for 2-3× faster JSON writes via `OPT_INDENT_2`
- **Unified UTC conversion**: `_target_utc_time(schedule, local_offset)` helper shared between `_is_due()` and `_time_to_next_due()`
- **LLM skill**: Users create/manage tasks via natural language in WhatsApp using the `task_scheduler` skill

---

## Execution Flow

```
Tick (every 30s)
     │
     ▼
For each chat_id, for each task:
     │
     ├─ _is_due()?
     │   ├─ enabled?
     │   ├─ schedule type check (time/interval/weekday)
     │   └─ same_day guard (prevent double-run)
     │
     ├─ _execute_task()
     │   ├─ Build prompt (inject compare + last_result)
     │   ├─ on_trigger → Bot.process_scheduled() → LLM
     │   ├─ Store result + update last_run
     │   └─ on_send → WhatsApp.deliver (with 2 retries)
     │
     └─ _persist() → workspace/<chat_id>/.scheduler/tasks.json
```

---

## Schedule Types

| Type | Config Fields | Runs |
|------|--------------|------|
| `daily` | `{hour, minute}` | Once per day at specified time |
| `interval` | `{seconds}` | Every N seconds |
| `cron` | `{hour, minute, weekdays}` | On specified days at specified time |

---

## Compare Mode

When `compare: true`:
- Previous run result injected into prompt
- LLM asked to highlight changes between runs
- Perfect for: price monitoring, news checks, status tracking

---

## Quick Example

```json
{
  "name": "morning-briefing",
  "schedule": {"type": "daily", "hour": 8, "minute": 0},
  "prompt": "Summarize today's calendar and weather",
  "compare": false,
  "enabled": true
}
```

---

## Codebase

- `src/scheduler.py` — TaskScheduler (background loop, evaluation, execution)
- `skills/builtin/task_scheduler.py` — LLM skill for CRUD via natural language

## Related

- `concepts/react-loop.md` — How scheduled tasks use the same LLM pipeline
- `lookup/workspace-structure.md` — Where `.scheduler/` lives per chat
- `lookup/built-in-skills.md` — task_scheduler skill reference
