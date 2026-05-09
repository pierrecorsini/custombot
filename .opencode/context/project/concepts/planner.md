<!-- Context: project/concepts/planner | Priority: medium | Version: 1.0 | Updated: 2026-04-16 -->

# Concept: Planner / Task Tracking

**Core Idea**: Create plans with tasks, manage dependencies between them, and determine execution order via topological sort. Users interact through natural language in WhatsApp — the planner skill handles CRUD operations and returns the next unblocked task.

**Source**: `FEATURES.md` — Planner section (archived 2026-04-16)

---

## Key Points

- **Per-chat storage**: Plans stored as JSON in `workspace/<chat_id>/.plans/<name>.json`
- **Dependency resolution**: Tasks declare `depends_on` — only tasks with all deps completed are "ready"
- **Topological sort**: `plan` action shows full execution order
- **`next` action**: Returns first unblocked task — enables step-by-step execution
- **6 actions**: `init`, `add`, `list`, `next`, `complete`, `status`, `plan`

---

## Dependency Model

```
Task A (done) ──▶ Task B (ready) ──▶ Task C (blocked by B)

"next" returns Task B (first task whose deps are all completed)
```

---

## Actions

| Action | Description |
|--------|-------------|
| `init` | Create a new plan |
| `add` | Add task with optional dependencies |
| `list` | Show all tasks + status |
| `next` | Show next unblocked task |
| `complete` | Mark task done with summary |
| `status` | Overall plan progress |
| `plan` | Show execution order (topological sort) |

---

## Quick Example

```
User: "Create a plan called 'launch-website'"
  → planner(action="init", name="launch-website")

User: "Add task 'design mockup'"
  → planner(action="add", task="design mockup")

User: "Add task 'implement frontend' that depends on 'design mockup'"
  → planner(action="add", task="implement frontend", depends_on=["design mockup"])

User: "What's next?"
  → planner(action="next") → "design mockup"

User: "Mark 'design mockup' done"
  → planner(action="complete", task="design mockup", summary="Created 3 mockups")
```

---

## Codebase

- `skills/builtin/planner.py` — Planner skill (all 6 actions)
- `workspace/whatsapp_data/<chat_id>/.plans/` — Per-chat plan JSON storage

## Related

- `lookup/workspace-structure.md` — Where `.plans/` directory lives
- `lookup/built-in-skills.md` — planner skill reference
