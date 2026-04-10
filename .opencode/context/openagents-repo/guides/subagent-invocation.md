<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: Subagent Invocation

**Core Idea**: The `subagent_type` parameter must use the exact `name` field from the agent's frontmatter — not the file path, not the kebab-case ID.

---

## Available Subagent Types

| Category | Types |
|----------|-------|
| Core | `"Task Manager"`, `"Documentation"`, `"ContextScout"` |
| Code | `"Coder Agent"`, `"TestEngineer"`, `"Reviewer"`, `"Build Agent"` |
| System Builder | `"Domain Analyzer"`, `"Agent Generator"`, `"Context Organizer"`, `"Workflow Designer"`, `"Command Creator"` |
| Utility | `"Image Specialist"` |

## Correct Format

```javascript
task(subagent_type="Task Manager", description="Break down feature", prompt="...")
```

## ❌ Incorrect Formats

```javascript
task(subagent_type="TaskManager")           // No spaces
task(subagent_type="task-manager")          // kebab-case ID
task(subagent_type=".opencode/agent/...")    // File path
```

---

## How to Find the Correct Name

1. Check registry: `cat registry.json | jq -r '.components.subagents[].name'`
2. Check frontmatter: Look at `name:` field in agent `.md` file

## ContextScout Workaround

If not registered in CLI, use direct operations instead:
```javascript
glob(pattern="**/*.md", path=".opencode/context")
grep(pattern="topic", path=".opencode/context")
```

## Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| "Unknown agent type" | Wrong format or not registered | Use exact name from registry |
| "Subagent not found" | File doesn't exist | Verify path, run validate-registry.sh |
| Silent failure | Missing tools or permissions | Check subagent tools config |

## Related

- `lookup/subagent-test-commands.md` — Test commands
- `errors/tool-permission-errors.md` — Permission issues
