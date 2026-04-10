<!-- Context: development/concepts | Priority: high | Version: 1.0 | Updated: 2026-03-20 -->

# Concept: Task History Persistence

**Purpose**: Persist execution history for scheduled tasks, enabling context-aware repeated executions.

---

## Core Idea

Scheduled tasks can optionally save their execution results to memory files. On subsequent runs, the task can access previous history, enabling cumulative learning and state tracking.

**Key Points**:
- `save_history: true` flag on task enables persistence
- History stored in `.workspace/<chat>/task_memory/<task_id>.md`
- Each execution appended with timestamp header
- Previous history injected into LLM context before execution

---

## Workflow

```
Task Trigger (save_history=true)
    ↓
1. Load .workspace/<chat>/task_memory/<task_id>.md
    ↓
2. Inject history into context
    ↓
3. Execute task command
    ↓
4. Append result to memory file
    ↓
## 2026-03-20 09:00:00
Result: Daily report content...

## 2026-03-21 09:00:00
Result: Daily report content...
```

---

## History Format

```markdown
## 2026-03-20 09:00:00

**Result**: Daily report generated...

---

## 2026-03-21 09:00:00

**Result**: Daily report generated...
```

---

## Use Cases

- **Daily reports**: Track metrics over time
- **Scheduled reminders**: Remember previous outcomes
- **Periodic checks**: Compare current vs. previous state
- **Learning tasks**: Build knowledge across runs

---

## Implementation

- **File**: `scheduler.py`
- **Methods**: `_load_task_history()`, `_save_task_history()`
- **DB Column**: `tasks.save_history` (INTEGER)

---

## Related

- `scheduler.py` - Implementation
- `skills/builtin/schedule.py` - Schedule skill with save_history
