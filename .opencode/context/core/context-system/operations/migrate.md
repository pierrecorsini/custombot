<!-- Context: core/migrate | Priority: medium | Version: 1.0 | Updated: 2026-02-15 -->

# Context Migrate Operation

**Purpose**: Copy project-intelligence from global (`~/.config/opencode/context/`) to local (`.opencode/context/`) for git-tracking and team-sharing

**Last Updated**: 2026-02-06

---

## Core Concept

Global project-intelligence files are project-specific but not committed to git. Migrate them to local so patterns are version-controlled and team-shared. Local overrides global; global remains as fallback for other projects.

---

## 4-Stage Workflow

### Stage 1: Detect Sources
Scan `~/.config/opencode/context/project-intelligence/`. Show files with versions and sizes. If no global context found → exit with message.

### Stage 2: Check for Conflicts
If local `.opencode/context/project-intelligence/` already exists:
- **Skip existing** — Only copy files that don't exist locally
- **Overwrite all** — Replace local with global (show diff first, backup local first)
- **Cancel**

### Stage 3: Approval & Copy
Show migration plan (source → destination, file list). On approval: create directory, copy files, validate MVI compliance.

### Stage 4: Cleanup & Confirmation
Show results. Optional: remove global `project-intelligence/` (keeps other global context intact). Default: keep global files.

---

## What Gets Migrated

| Migrated (project-specific) | NOT Migrated (universal) |
|---|---|
| `project-intelligence/technical-domain.md` | `core/standards/` |
| `project-intelligence/business-domain.md` | `core/context-system/` |
| `project-intelligence/navigation.md` | `core/guides/` |
| `project-intelligence/decisions-log.md` | Any other `core/` files |
| `project-intelligence/living-notes.md` | |

**Rationale**: Project intelligence = YOUR stack, YOUR patterns. Core standards = universal quality rules.

---

## Error Handling

- **Permission denied**: Check `.opencode/context/` write permissions
- **Global path not found**: Set `OPENCODE_INSTALL_DIR` env var for custom install locations

---

## Related

- `/add-context` — Create new project intelligence
- `/context harvest` — Extract knowledge from summaries
