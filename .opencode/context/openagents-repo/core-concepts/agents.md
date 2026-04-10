<!-- Context: openagents-repo/core-concepts/agents | Priority: critical | Version: 1.0 | Updated: 2026-01-13 -->
# Core Concept: Agents

Agents are AI prompt files (Markdown + YAML frontmatter) that define specialized behaviors for different tasks. They are category-organized by domain, context-aware, and validated through the eval framework.

---

## Key Points

- **Markdown files** with YAML frontmatter + prompt instructions
- **Category-organized**: `core/`, `subagents/development/`, `content/`, `data/`
- **Context-aware**: Load relevant context files via `<!-- Context: ... -->` comments
- **Testable**: Validated through eval framework (see `core-concepts/evals.md`)

---

## Agent Structure

```yaml
---
description: "Brief description of what this agent does"
mode: primary
tools:
  read: true
  write: true
permission:
  bash:
    "*": ask
---

# Agent prompt content — instructions, workflows, constraints
```

> Registry metadata (id, name, category, tags, dependencies) is stored separately in `.opencode/config/agent-metadata.json`. See `core-concepts/agent-metadata.md`.

---

## Categories & Subagents

| Category | Path | Key Agents |
|----------|------|------------|
| Core | `agent/core/` | openagent, opencoder, system-builder |
| Dev Subagents | `agent/subagents/development/` | frontend-specialist, devops-specialist |
| Code Subagents | `agent/subagents/code/` | tester, reviewer, coder-agent, build-agent |
| Core Subagents | `agent/subagents/core/` | task-manager, documentation |
| System Builder | `agent/subagents/system-builder/` | agent-generator, command-creator, domain-analyzer |
| Content | `agent/content/` | copywriter, technical-writer |
| Data | `agent/data/` | data-analyst |

### Agents vs Subagents

| | Category Agents | Subagents |
|---|---|---|
| **Invoked by** | User directly | Other agents via task tool |
| **Scope** | Broad domain | Narrow subtask |

---

## Path Resolution

1. Path has `/` → use as category path
2. No `/` → check `core/` first (backward compat)
3. Not in core/ → search all categories
4. Not found → error

---

## Agent Lifecycle

```bash
# 1. Create
touch .opencode/agent/{category}/{agent-name}.md

# 2. Test
cd evals/framework && npm run eval:sdk -- --agent={category}/{agent-name}

# 3. Register
./scripts/registry/auto-detect-components.sh --auto-add && ./scripts/registry/validate-registry.sh

# 4. Distribute
./install.sh {profile}
```

---

## Related Files

- **Agent metadata**: `core-concepts/agent-metadata.md`
- **Evals**: `core-concepts/evals.md`
- **Registry**: `core-concepts/registry.md`
- **Categories**: `core-concepts/categories.md`
- **Adding agents**: `guides/adding-agent.md`
- **Testing agents**: `guides/testing-agent.md`
- **File locations**: `lookup/file-locations.md`

---

**Last Updated**: 2026-01-13 | **Version**: 0.5.1
