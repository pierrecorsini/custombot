<!-- Context: openagents-repo/errors | Priority: medium | Version: 2.0 | Updated: 2026-02-15 -->

# Tool Permission Errors

**Core Idea**: Agents get blocked when tools are disabled/denied in frontmatter. Fix by either emphasizing restrictions in prompt or enabling the tool.

---

## Error: Tool Permission Denied

**Symptom**: `missing-approval: Execution tool 'bash' called without requesting approval` or 0 tool calls.

**Cause**: Tool disabled in frontmatter:
```yaml
tools:
  bash: false
permissions:
  bash:
    "*": "deny"
```

**Fix 1 (Recommended)**: Add critical rules in prompt:
```xml
<critical_rules priority="absolute">
  <rule id="tool_usage">
    ONLY use: glob, read, grep, list
    NEVER use: bash, write, edit, task
  </rule>
</critical_rules>
```

**Fix 2**: Enable tool if agent legitimately needs it:
```yaml
tools:
  bash: true
```

---

## Error: Subagent Approval Gate Violation

**Symptom**: Subagent test fails with approval error.

**Cause**: Subagents shouldn't have approval gates — they're delegated by primary agents.

**Fix**: Use `auto-approve` in test config:
```yaml
approvalStrategy:
  type: auto-approve
```

---

## Tool Permission Matrix

| Agent Type | bash | write | edit | task | read | grep | glob |
|------------|------|-------|------|------|------|------|------|
| Read-only subagent | ❌ | ❌ | ❌ | ❌ | ✅ | ✅ | ✅ |
| Primary agent | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

## Verification

- [ ] Frontmatter has correct `tools:` config?
- [ ] Prompt emphasizes allowed tools in critical rules?
- [ ] Subagent tests use `auto-approve`?

## Related

- `concepts/subagent-testing-modes.md` — Testing modes
- `examples/subagent-prompt-structure.md` — Prompt structure
