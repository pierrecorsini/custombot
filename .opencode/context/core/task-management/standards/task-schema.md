<!-- Context: core/task-schema | Priority: critical | Version: 1.0 | Updated: 2026-02-15 -->

# Standard: Task JSON Schema

**Purpose**: JSON schema reference for task management files

**Last Updated**: 2026-02-14

---

## Core Concepts

- `task.json` — Feature-level metadata and tracking
- `subtask_NN.json` — Individual atomic tasks with dependencies
- Location: `.tmp/tasks/{feature-slug}/` (project root)

Enhanced fields (line precision, domain modeling, contracts, ADRs) are optional extensions to the base schema.

---

## task.json Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | kebab-case identifier |
| `name` | string | Yes | Human-readable (max 100) |
| `status` | enum | Yes | active / completed / blocked / archived |
| `objective` | string | Yes | One-line objective (max 200) |
| `context_files` | array | No | Standards/conventions from `.opencode/context/` |
| `reference_files` | array | No | Project source files to reference |
| `exit_criteria` | array | No | Completion conditions |
| `subtask_count` | int | No | Total subtasks |
| `completed_count` | int | No | Done subtasks |
| `created_at` | datetime | Yes | ISO 8601 |
| `completed_at` | datetime | No | ISO 8601 |

---

## subtask_NN.json Schema

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `id` | string | Yes | {feature}-{seq} |
| `seq` | string | Yes | 2-digit (01, 02) |
| `title` | string | Yes | Task title (max 100) |
| `status` | enum | Yes | pending / in_progress / completed / blocked |
| `depends_on` | array | No | Dependency sequence numbers |
| `parallel` | bool | No | Can run alongside others |
| `context_files` | array | No | Standards to follow |
| `reference_files` | array | No | Existing files to reference |
| `suggested_agent` | string | No | Recommended agent |
| `acceptance_criteria` | array | No | Binary pass/fail conditions |
| `deliverables` | array | No | Files to create/modify |
| `completion_summary` | string | No | What was done (max 200) |

---

## Status Transitions

```
pending → in_progress → completed
  * → blocked → pending (when unblocked)
```

---

## context_files vs reference_files

| Field | Answers | Contains |
|-------|---------|----------|
| `context_files` | "What rules do I follow?" | Standards from `.opencode/context/` |
| `reference_files` | "What existing code do I look at?" | Project source files, configs |

**Never mix them** — clean separation between standards and source material.

---

## Related

- `task-schema.md` — This file (base + extended fields)
- `../lookup/task-commands.md` — CLI reference
- `../guides/managing-tasks.md` — Lifecycle workflow
