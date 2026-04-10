<!-- Context: openagents-repo/examples | Priority: high | Version: 2.0 | Updated: 2026-02-15 -->

# Subagent Prompt Structure (Optimized)

**Core Idea**: Critical instructions (tool usage) must be in first 15% of prompt. Use 3-tier execution priority, ≤4 nesting levels, explicit "What NOT to Do" section.

---

## Optimized Template

```xml
---
id: subagent-name
name: Subagent Name
category: subagents/core
type: subagent
mode: subagent
tools:
  read: true
  grep: true
  glob: true
  bash: false
  edit: false
  write: false
---

# Agent Name

> **Mission**: One-sentence mission statement

<!-- CRITICAL: Must be in first 15% of prompt -->
<critical_rules priority="absolute" enforcement="strict">
  <rule id="tool_usage">
    ONLY use: glob, read, grep, list
    NEVER use: bash, write, edit, task
  </rule>
  <rule id="always_use_tools">
    ALWAYS use tools to discover/verify
    NEVER assume or fabricate information
  </rule>
</critical_rules>

<execution_priority>
  <tier level="1" desc="Critical">Tool usage, verification</tier>
  <tier level="2" desc="Core">Main workflow steps</tier>
  <tier level="3" desc="Quality">Validation, edge cases</tier>
  <conflict_resolution>Tier 1 always overrides Tier 2/3</conflict_resolution>
</execution_priority>

## What NOT to Do
- ❌ NEVER use bash/write/edit/task tools
- ❌ Don't assume—verify with tools
```

---

## Key Optimizations

| Optimization | Before | After | Impact |
|---|---|---|---|
| Critical rules early | Line 596 | Line 50 | Tool usage emphasized |
| 3-Tier priority | None | Explicit | Conflict resolution |
| Nesting depth | 6-7 levels | ≤4 levels | Better clarity |
| Negative examples | None | "What NOT to Do" | Prevents mistakes |

## Real Result (ContextScout)

- **Before**: 750 lines, rules at line 596 → **After**: 394 lines (47.5% reduction)
- Tests went from failing (0 tool calls) to passing

## File Size Target

Frontmatter 30-50 + Critical Rules 20-30 + Context 20-30 + Priority 20-30 + Workflow 80-120 = **<400 lines**

## Related

- `concepts/subagent-testing-modes.md` — Testing modes
- `guides/testing-subagents.md` — Testing guide
- `errors/tool-permission-errors.md` — Permission issues
