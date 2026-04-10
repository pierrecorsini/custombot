<!-- Context: workflows/external-context-integration | Priority: high | Version: 1.0 | Updated: 2026-01-28 -->
# External Context Integration Guide

## Overview

How to integrate external context (fetched via ExternalScout) into the agent workflow so subagents access it without re-fetching.

**Key Principle**: Main agents fetch external docs once → persist to disk → reference in session → subagents read (no re-fetching)

---

## When to Use

- User asks about **external libraries** (Drizzle, Better Auth, Next.js, etc.)
- Task involves **integration** between multiple external libraries
- **Setup or configuration** of external tools is needed

**Don't use** when: question is about internal project code, answer is in `.opencode/context/`, or general programming concepts.

---

## Integration Workflow

1. **Analyze & Discover**: Analyze user request → Identify external libraries → Call ContextScout (internal) + ExternalScout (external) → ExternalScout persists to `.tmp/external-context/` → Capture returned file paths
2. **Propose Plan**: Show user summary (what will be done, which libraries, which context files) → Include discovered external context → Wait for approval
3. **Init Session**: Create `.tmp/sessions/{session-id}/context.md` → Populate with context files, reference files, and **"## External Context Fetched"** section listing file paths from ExternalScout
4. **Delegate**: Call TaskManager with session path → TaskManager reads session context → Extracts external context files → Includes in subtask JSONs
5. **Subagents Execute**: Read session context → Read external context files from `.tmp/external-context/` → Implement using external docs → **NO RE-FETCHING**

---

## Minimal Example

```markdown
## External Context Fetched (in session context.md)

### Drizzle ORM
- `.tmp/external-context/drizzle-orm/modular-schemas.md` — Schema organization
- `.tmp/external-context/drizzle-orm/postgresql-setup.md` — PostgreSQL config

**Important**: These files are read-only. Do not modify.
```

---

## Best Practices

**Main Agents** ✅: Call ExternalScout early, capture file paths, add to session context, pass session path to subagents. ❌: Skip ExternalScout, re-fetch docs, modify external context files.

**ExternalScout** ✅: Persist to `.tmp/external-context/`, update `.manifest.json`, filter aggressively, cite sources. ❌: Skip persistence, return entire docs, fabricate content.

**Subagents** ✅: Read external_context from subtask JSON, reference in implementation. ❌: Re-fetch, ignore external context, modify cached files.

---

## Troubleshooting

- **Files not found**: Verify ExternalScout ran, check path in session context matches actual location, check `.manifest.json`
- **Stale context**: Delete stale files, re-run ExternalScout, update session context with new paths
- **Manifest out of sync**: Regenerate manifest by listing actual files in `.tmp/external-context/`

---

## References

- **ExternalScout**: `.opencode/agent/subagents/core/externalscout.md`
- **External Context Management**: `.opencode/context/core/workflows/external-context-management.md`
- **Task Delegation**: `.opencode/context/core/workflows/task-delegation-basics.md`
