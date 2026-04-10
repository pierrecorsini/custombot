<!-- Context: openagents-repo/core-concepts/agent-metadata | Priority: critical | Version: 1.0 | Updated: 2026-01-31 -->
# Core Concept: Agent Metadata System

Agent metadata is separated from agent frontmatter to prevent OpenCode validation errors. Agent files contain ONLY valid OpenCode fields; all other metadata (id, name, category, tags, dependencies) lives in a centralized JSON file.

---

## Key Points

- **Agent frontmatter**: Only `description`, `mode`, `model`, `temperature`, `maxSteps`, `disable`, `prompt`, `hidden`, `tools`, `permission`
- **Metadata file**: `.opencode/config/agent-metadata.json` holds id, name, category, type, version, author, tags, dependencies
- **Auto-detect merges both**: `scripts/registry/auto-detect-components.sh` reads frontmatter + metadata → registry.json
- **v1.1.1+**: Use `permission:` (singular), not `permissions:` (plural, deprecated)

---

## Valid OpenCode Frontmatter Fields

| Field | Required | Description |
|-------|----------|-------------|
| `description` | Yes | When to use this agent |
| `mode` | Yes | `primary`, `subagent`, or `all` |
| `model` | No | Model override (e.g., `anthropic/claude-sonnet-4-20250514`) |
| `temperature` | No | Response randomness (0.0-1.0) |
| `maxSteps` | No | Max agentic iterations |
| `tools` | No | Tool access config (`read: true`, `write: false`) |
| `permission` | No | Permission rules per tool (v1.1.1+, singular) |
| `disable` | No | Set `true` to disable |
| `hidden` | No | Hide from @ autocomplete (subagents) |
| `prompt` | No | Custom prompt file path |

---

## Metadata Schema (`agent-metadata.json`)

| Field | Required | Description |
|-------|----------|-------------|
| `id` | Yes | Unique identifier (kebab-case) |
| `name` | Yes | Display name |
| `category` | Yes | Agent category (`core`, `development`, `content`, etc.) |
| `type` | Yes | `agent` or `subagent` |
| `version` | Yes | Semantic version (`"1.0.0"`) |
| `author` | Yes | Author identifier |
| `tags` | No | Discovery tags array |
| `dependencies` | No | Array of `type:id` strings |

### Minimal Example

```json
// .opencode/config/agent-metadata.json
{
  "agents": {
    "my-agent": {
      "id": "my-agent", "name": "My Agent",
      "category": "development", "type": "subagent",
      "version": "1.0.0", "author": "opencode",
      "tags": ["custom"], "dependencies": ["subagent:tester"]
    }
  }
}
```

---

## Workflow

```bash
# 1. Create agent (OpenCode fields only)
vim .opencode/agent/{category}/{agent}.md

# 2. Add metadata entry
vim .opencode/config/agent-metadata.json

# 3. Update registry
./scripts/registry/auto-detect-components.sh --auto-add

# 4. Validate
./scripts/registry/validate-registry.sh
```

---

## Related Files

- **OpenCode Agent Docs**: https://opencode.ai/docs/agents/
- **Registry System**: `core-concepts/registry.md`
- **Adding Agents**: `guides/adding-agent-basics.md`
- **Dependencies**: `quality/registry-dependencies.md`

---

**Last Updated**: 2026-01-31 | **Version**: 1.0.0
