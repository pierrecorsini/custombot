<!-- Context: openagents-repo/core-concepts/categories | Priority: high | Version: 1.0 | Updated: 2026-01-13 -->
# Core Concept: Category System

Categories are domain-based groupings that organize agents, context files, and tests by expertise area. They enable scalability, discovery, and modular installation.

---

## Key Points

- **Domain-based organization**: Agents grouped by expertise (core, development, content, data, etc.)
- **Parallel structure**: Same category used across agents, context, prompts, and evals
- **Category metadata**: Each category has a `0-category.json` with name, description, icon, order, status
- **Path resolution**: Short IDs resolve backward-compatibly; new agents use category paths

---

## Available Categories

| Category | Path | Agents | Status |
|----------|------|--------|--------|
| Core | `core/` | openagent, opencoder | ✅ Stable |
| Development (subagents) | `subagents/development/` | frontend-specialist, devops-specialist | ✅ Active |
| Content | `content/` | copywriter, technical-writer | ✅ Active |
| Data | `data/` | data-analyst | ✅ Active |
| Product | `product/` | (none yet) | 🔜 Ready |
| Learning | `learning/` | (none yet) | 🔜 Ready |

---

## Directory Structure

```
.opencode/
├── agent/{category}/           # Agents + 0-category.json
├── context/{category}/         # Context files + navigation.md
├── prompts/{category}/         # Model-specific prompt variants
evals/agents/{category}/        # Tests
```

---

## Category Metadata (`0-category.json`)

```json
{
  "name": "Development",
  "description": "Software development specialists",
  "icon": "💻",
  "order": 2,
  "status": "active"
}
```

---

## Path Resolution

| Input | Resolves To |
|-------|-------------|
| `"openagent"` | `.opencode/agent/core/openagent.md` (backward compat) |
| `"core/openagent"` | `.opencode/agent/core/openagent.md` |
| `"subagents/development/frontend-specialist"` | `.opencode/agent/subagents/development/frontend-specialist.md` |

Rules: (1) Has `/` → category path, (2) No `/` → check core/ first, (3) Search all categories.

---

## Naming Conventions

- **Categories**: lowercase, singular (`development`, `content`)
- **Agents**: kebab-case (`frontend-specialist.md`)
- **Context**: kebab-case, one topic per file (`react-patterns.md`)

---

## Related Files

- **Adding agents**: `guides/adding-agent.md`
- **Adding categories**: `guides/add-category.md`
- **Agent concepts**: `core-concepts/agents.md`
- **File locations**: `lookup/file-locations.md`

---

**Last Updated**: 2026-01-13 | **Version**: 0.5.1
