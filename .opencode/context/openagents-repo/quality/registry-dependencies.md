<!-- Context: quality/registry-dependencies | Priority: high | Version: 1.0 | Updated: 2026-01-06 -->

# Registry Dependency Validation

**Core Idea**: All component dependencies must be declared in frontmatter using `type:id` format and validated before commits.

---

## Dependency Types

| Type | Format | Example |
|------|--------|---------|
| agent | `agent:id` | `agent:opencoder` |
| subagent | `subagent:id` | `subagent:coder-agent` |
| command | `command:id` | `command:context` |
| context | `context:path` | `context:core/standards/code` |

## Critical Commands

```bash
/check-context-deps              # Analyze context dependencies
/check-context-deps --fix        # Auto-fix missing dependencies
./scripts/registry/auto-detect-components.sh --auto-add  # Update registry
./scripts/registry/validate-registry.sh                   # Validate
```

---

## Declaring Dependencies

```yaml
id: opencoder
dependencies:
  - subagent:task-manager
  - context:core/standards/code   # Path: no .opencode/, no .md
```

## Validation Workflow

1. **Check context deps**: `/check-context-deps`
2. **Fix missing**: `/check-context-deps --fix`
3. **Update registry**: `auto-detect-components.sh --auto-add`
4. **Validate**: `validate-registry.sh`

## Context Path Normalization

```
File: .opencode/context/core/standards/code-quality.md
Dependency: context:core/standards/code   (no prefix, no extension)
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Missing context dep | `/check-context-deps --fix` |
| Dependency not in registry | `auto-detect-components.sh --auto-add` |
| Unused context files | Add to agent or remove file |
| Circular deps | Extract shared logic to third component |

## CI/CD Integration

```yaml
- name: Validate Registry
  run: ./scripts/registry/validate-registry.sh
- name: Check Context Dependencies
  run: /check-context-deps
```

## Related

- `guides/updating-registry.md` — Registry management guide
- `core-concepts/registry.md` — Registry concepts
