<!-- Context: openagents-repo/context-bundle-template | Priority: low | Version: 2.0 | Updated: 2026-02-15 -->

# Context Bundle Template

**Core Idea**: When delegating to subagents, create a context bundle at `.tmp/context/{session-id}/bundle.md` with task overview, context files to load, requirements, and success criteria.

---

## Template Structure

```markdown
# Context Bundle: {Task Name}
Session: {session-id} | For: {subagent-name} | Status: in_progress

## Task Overview
{Brief description}

## Context Files (Load Before Starting)
**Core Standards**:
- .opencode/context/core/standards/code-quality.md
- .opencode/context/core/standards/test-coverage.md

**Repo Context**:
- .opencode/context/openagents-repo/quick-start.md
- .opencode/context/openagents-repo/core-concepts/{relevant}.md

## Key Requirements
- {requirement 1 from standards}
- {requirement 2 from repo context}

## Files to Create/Modify
**Create**: {file-path-1} - {purpose}
**Modify**: {file-path-2} - {what changes}

## Success Criteria
- [ ] {binary pass/fail condition 1}
- [ ] {binary pass/fail condition 2}

## Validation
- `./scripts/registry/validate-registry.sh`
- `cd evals/framework && npm run eval:sdk -- --agent={agent}`

## Instructions for Subagent
{Specific task instructions}
```

---

## How to Use

```bash
# 1. Create session dir
mkdir -p .tmp/context/{session-id}

# 2. Copy template and fill in placeholders

# 3. Pass to subagent
task(
  subagent_type="{SubagentName}",
  description="Brief description",
  prompt="Load context from .tmp/context/{session-id}/bundle.md before starting."
)
```

## Best Practices

- ✅ Reference context files by path (don't duplicate content)
- ✅ Binary success criteria (pass/fail, not "make it good")
- ✅ Include validation commands
- ❌ Don't duplicate full context content
- ❌ Don't skip validation requirements

## Example

See `openagents-repo/examples/context-bundle-example.md`

## Related

- `templates/context-bundle-template.md` — Duplicate template
- `examples/context-bundle-example.md` — Complete example
