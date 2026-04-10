<!-- Context: openagents-repo/context-bundle-template | Priority: low | Version: 2.0 | Updated: 2026-02-15 -->

# Context Bundle Template

**Purpose**: Template for delegating tasks to subagents with full context
**Location**: `.tmp/context/{session-id}/bundle.md`

---

## Template

```markdown
# Context Bundle: {Task Name}
Session: {session-id} | For: {subagent-name} | Status: in_progress

## Task Overview
{Brief description}

## Context Files to Load
**Standards**: core/standards/code-quality.md, test-coverage.md, documentation.md
**Repo Context**: openagents-repo/quick-start.md, core-concepts/{relevant}.md
**Guides**: openagents-repo/guides/{relevant}-basics.md

## Key Requirements
- {requirement 1}
- {requirement 2}

## Files to Create/Modify
**Create**: {path} - {purpose}
**Modify**: {path} - {changes}

## Success Criteria
- [ ] {criterion 1}
- [ ] {criterion 2}

## Validation
- ./scripts/registry/validate-registry.sh
- cd evals/framework && npm run eval:sdk -- --agent={agent}
```

---

## Usage

```bash
mkdir -p .tmp/context/{session-id}
# Fill template, then:
task(subagent_type="{Name}", prompt="Load .tmp/context/{session-id}/bundle.md")
```

## Best Practices

- ✅ Reference files by path, don't duplicate content
- ✅ Binary pass/fail criteria
- ✅ Include validation commands
- ❌ Don't use vague criteria

## Related

- `blueprints/context-bundle-template.md` — Blueprint version
- `examples/context-bundle-example.md` — Complete example
