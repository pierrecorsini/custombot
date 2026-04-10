<!-- Context: core/task-commands | Priority: high | Version: 1.0 | Updated: 2026-02-15 -->

# Lookup: Task CLI Commands

**Purpose**: Quick reference for task-cli.ts commands

**Last Updated**: 2026-02-14

---

## Usage

```bash
npx ts-node .opencode/context/tasks/scripts/task-cli.ts <command> [args]
```

Task files: `.tmp/tasks/` (project root)

---

## Command Reference

| Command | Args | Description |
|---------|------|-------------|
| `status` | `[feature]` | Show task summary for all/specific features |
| `next` | `[feature]` | Show tasks ready to work (deps satisfied) |
| `parallel` | `[feature]` | Show only parallelizable ready tasks |
| `deps` | `<feature> <seq>` | Show dependency tree for a task |
| `blocked` | `[feature]` | Show blocked tasks and reasons |
| `complete` | `<feature> <seq> "summary"` | Mark task completed (max 200 chars) |
| `validate` | `[feature]` | Check JSON validity, deps, circular refs |

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Error (validation failed, missing args) |

---

## Enhanced Schema Support

CLI supports v2.0 schema (all optional, backward compatible):
- Line-number precision, domain modeling (bounded_context, module)
- Contract tracking, design artifacts, ADR references
- RICE/WSJF prioritization scores

See `../standards/task-schema.md` for details.

---

## Planning Agent Integration

| Agent | Output |
|-------|--------|
| ArchitectureAnalyzer | `.tmp/architecture/contexts.json` |
| StoryMapper | `.tmp/story-maps/map.json` |
| PrioritizationEngine | `.tmp/backlog/prioritized.json` |
| ContractManager | `.tmp/contracts/{service}.json` |
| ADRManager | `docs/adr/` |

---

## Related

- `../standards/task-schema.md` — Base JSON schema
- `../standards/task-schema.md` — Full JSON schema
- `../guides/managing-tasks.md` — Workflow guide
