<!-- Context: openagents-repo/registry | Priority: high | Version: 1.0 | Updated: 2026-02-15 -->
# Core Concept: Registry System

The registry is a centralized catalog (`registry.json` at repo root) that tracks all components — agents, subagents, commands, tools, and contexts — with auto-detect scanning, dependency resolution, and profile-based installation.

---

## Key Points

- **Centralized catalog**: All components registered with id, path, metadata, dependencies
- **Auto-detect**: Scans `.opencode/` and auto-generates registry entries from frontmatter + metadata
- **Profiles**: Pre-configured bundles (`essential`, `developer`, `business`) for quick install
- **Dependency resolution**: Recursively resolves `type:id` dependency chains

---

## Registry Schema

```json
{
  "version": "0.5.0",
  "schema_version": "2.0.0",
  "components": {
    "agents": [...], "subagents": [...],
    "commands": [...], "tools": [...], "contexts": [...]
  },
  "profiles": { "essential": {...}, "developer": {...}, "business": {...} }
}
```

### Component Entry Fields

| Field | Description |
|-------|-------------|
| `id` | Unique identifier (kebab-case) |
| `name` | Display name |
| `type` | `agent`, `subagent`, `command`, `tool`, or `context` |
| `path` | File path relative to repo root |
| `description` | Brief description |
| `category` | Category name (for agents) |
| `tags` | Discovery tags |
| `dependencies` | Array of `type:id` strings |
| `version` | Version when added/updated |

---

## Auto-Detect Commands

```bash
./scripts/registry/auto-detect-components.sh --dry-run      # Preview changes
./scripts/registry/auto-detect-components.sh --auto-add     # Add components
./scripts/registry/auto-detect-components.sh --auto-add --force  # Force update
./scripts/registry/validate-registry.sh                     # Validate registry
./scripts/registry/validate-registry.sh -v                  # Verbose
```

### Detection Paths

| Type | Path Pattern |
|------|-------------|
| Agents | `.opencode/agent/{category}/*.md` |
| Subagents | `.opencode/agent/subagents/**/*.md` |
| Commands | `.opencode/command/**/*.md` |
| Tools | `.opencode/tool/**/index.ts` |
| Contexts | `.opencode/context/**/*.md` |

---

## Profiles

| Profile | Description | Key Components |
|---------|-------------|----------------|
| `essential` | Minimal setup | core agents, commit/test commands |
| `developer` | Full dev setup | all agents + subagents + commands + dev context |
| `business` | Content/product focus | core agents + content specialists + content context |

---

## Dependency Resolution

```
frontend-specialist → subagent:tester → context:core/standards/tests
Install order: 1. context  2. subagent  3. agent
```

---

## Related Files

- **Updating registry**: `guides/updating-registry.md`
- **Adding agents**: `guides/adding-agent.md`
- **Categories**: `core-concepts/categories.md`

---

**Last Updated**: 2025-01-28 | **Version**: 0.5.2
