<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Guide: Updating Registry

**Core Idea**: Use auto-detect scripts to keep registry.json in sync with component files. Prefer frontmatter metadata over manual edits.

---

## Quick Commands

```bash
./scripts/registry/auto-detect-components.sh --dry-run    # Preview changes
./scripts/registry/auto-detect-components.sh --auto-add   # Apply changes
./scripts/registry/validate-registry.sh                   # Validate
```

## When to Update

- Add/remove/rename agents, commands, tools, or context files
- Change component metadata (tags, dependencies, description)

---

## Auto-Detect Workflow (Recommended)

1. Create component file with frontmatter:
```yaml
---
description: "Brief description"
tags: [tag1, tag2]
dependencies: [subagent:coder-agent, context:core/standards/code]
---
```

2. Apply: `auto-detect-components.sh --auto-add`
3. Validate: `validate-registry.sh`

## Dependency Format

```yaml
dependencies:
  - subagent:coder-agent          # type:id format
  - context:core/standards/code   # no .opencode/, no .md
  - command:context
```

---

## Manual Updates (Last Resort)

Only edit `registry.json` directly if auto-detect fails. Validate after any manual edit.

## Aliases

Add aliases manually in `registry.json` for backward compatibility:
```json
{ "id": "session-management", "aliases": ["workflows-sessions", "sessions"] }
```

---

## Validation Errors

| Error | Fix |
|-------|-----|
| Path not found | Fix path or remove entry |
| Duplicate ID | Rename one component |
| Missing dependency | Add component or remove reference |
| Invalid category | Use valid category |

## Best Practices

- ✅ Use frontmatter for metadata, not direct registry edits
- ✅ Dry-run first, then apply
- ✅ Validate after every change
- ✅ Multi-line YAML arrays for readability

## Related

- `core-concepts/registry.md` — Registry concepts
- `quality/registry-dependencies.md` — Dependency validation
