<!-- Context: workflows/external-context | Priority: high | Version: 1.0 | Updated: 2026-01-28 -->
# External Context Management

## Overview

External context = live documentation fetched from external libraries (via Context7 API). We **persist** to `.tmp/external-context/` so agents can pass it to subagents without re-fetching.

**Key Principle**: ExternalScout fetches once → persists to disk → main agents reference → subagents read

---

## Directory Structure

```
.tmp/external-context/
├── .manifest.json              # Metadata for all cached docs
├── {package-name}/             # Exact npm name (kebab-case)
│   ├── {topic}.md              # Kebab-case topic file
│   └── ...
```

**Naming**: Package dirs = exact npm name (`drizzle-orm`, `better-auth`). Files = kebab-case topics (`modular-schemas.md`).

---

## Manifest & File Format

**Manifest** (`.manifest.json`): Tracks cached packages with `files[]`, `last_updated`, `source`, `official_docs` URL per package.

**File template**:
```markdown
---
source: Context7 API
library: {Name}
package: {npm-name}
topic: {topic}
fetched: {ISO timestamp}
official_docs: {URL}
---

# {Topic} in {Library}
[Filtered documentation content — relevant sections only]
```

---

## Workflow

1. **Main Agent** detects external libraries needed → calls ExternalScout
2. **ExternalScout** fetches from Context7 API → filters to relevant sections → persists to `.tmp/external-context/{package}/{topic}.md` → updates `.manifest.json` → returns file paths
3. **Main Agent** creates session context with "## External Context Fetched" section listing paths
4. **Subagents** read external context files from session → implement using docs → no re-fetching

---

## Integration with Task Delegation

**Session context** must include `## External Context Fetched` with package-grouped file paths.

**Subtask JSONs** include `external_context` array:
```json
{
  "context_files": ["standards/code-quality.md"],
  "reference_files": ["src/db/schema.ts"],
  "external_context": [
    ".tmp/external-context/drizzle-orm/modular-schemas.md"
  ]
}
```

---

## Cleanup & Maintenance

- Clean up when: task complete, docs stale (>7 days), user requests, disk space needed
- Delete package: `rm -rf .tmp/external-context/{package}/` → update `.manifest.json`
- Regenerate manifest: list actual `.md` files → update JSON to match

---

## Best Practices

- **Main Agents**: Call ExternalScout early, capture paths, add to session context, don't re-fetch
- **ExternalScout**: Always persist, update manifest, filter aggressively, include metadata headers
- **Subagents**: Read from session context, don't re-fetch, don't modify, pass to downstream agents

---

## Troubleshooting

- **Files not found**: Verify ExternalScout ran, check paths match, inspect `.manifest.json`
- **Stale docs**: Delete package dir → re-run ExternalScout → update session context
- **Manifest out of sync**: List actual files, regenerate manifest to match

---

## References

- **ExternalScout**: `.opencode/agent/subagents/core/externalscout.md`
- **Task Delegation**: `.opencode/context/core/workflows/task-delegation-basics.md`
- **Session Management**: `.opencode/context/core/workflows/session-management.md`
