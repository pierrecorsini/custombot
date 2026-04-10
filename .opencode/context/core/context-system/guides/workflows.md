<!-- Context: core/workflows | Priority: high | Version: 1.0 | Updated: 2026-02-15 -->

# Context Operation Workflows

**Purpose**: Step-by-step reference for all context operations

**Last Updated**: 2026-01-06

---

## Extract Workflow (7 Stages)

1. **Read Source** — Read URL/file/codebase, analyze for extractable items
2. **Analyze & Categorize** — Map content to functions: concepts/, examples/, guides/, lookup/, errors/
3. **Select Category** *(APPROVAL)* — User picks target category and items via letter selection (e.g., `A B C 1`)
4. **Preview** *(APPROVAL)* — Show files to create, conflicts, navigation updates. Options: preview content / approve / skip
5. **Create** — Write MVI-formatted files to function folders (<200 lines each)
6. **Update Navigation** — Update navigation.md with new entries, priorities, cross-references
7. **Report** — Summary: files created, navigation updated

**Usage**: `/context extract from {url|file|directory}`

---

## Organize Workflow (8 Stages)

1. **Scan** — Detect all files and current structure (flat vs organized)
2. **Categorize** — Map each file to function folder; flag ambiguous files
3. **Resolve Conflicts** *(APPROVAL)* — Handle ambiguous files (split/keep/skip) and duplicate targets (merge/rename/skip)
4. **Preview** *(APPROVAL)* — Show: CREATE dirs, MOVE files, SPLIT ambiguous, MERGE conflicts, UPDATE references
5. **Backup** — Save to `.tmp/backup/organize-{category}-{timestamp}/`
6. **Execute** — Create folders, move files, split/merge as approved
7. **Update** — Fix all internal references, update navigation.md
8. **Report** — Summary: files organized, folders created, references fixed

**Usage**: `/context organize {category}/` or `/context organize {category}/ --dry-run`

---

## Update Workflow (8 Stages)

1. **Identify Changes** *(APPROVAL)* — User describes what changed (API/deprecation/feature/breaking)
2. **Find Affected Files** — Grep for references, show impact analysis with line counts
3. **Preview Changes** *(APPROVAL)* — Line-by-line diff for each file. Options: yes/no/edit (line-by-line approval)
4. **Backup** — Save to `.tmp/backup/update-{topic}-{timestamp}/`
5. **Update Files** — Apply approved changes, maintain MVI format
6. **Add Migration Notes** — Append migration section to `errors/{topic}-errors.md`
7. **Validate** — Check references, links, file sizes
8. **Report** — Summary: files updated, references modified, backup location

**Usage**: `/context update for {topic}`

---

## Error Workflow (6 Stages)

1. **Search Existing** — Find similar/related errors via fuzzy match
2. **Check Duplication** *(APPROVAL)* — Options: add new / update existing / skip
3. **Preview** *(APPROVAL)* — Show full before/after error entry with diff markers
4. **Add/Update** — Write error entry following template format
5. **Update Navigation** — Add cross-references to related errors
6. **Report** — Summary: error added/updated, cross-references, file size

**Usage**: `/context error for "{error message}"`

---

## Harvest Workflow (6 Stages)

1. **Scan** — Find summary files (OVERVIEW.md, SESSION-*.md, etc.) in workspace
2. **Analyze** — Categorize content by function (concepts/examples/guides/errors/lookup)
3. **Approve** *(APPROVAL)* — Letter-based selection: `A B C` for specific, `all`, `none`, `cancel`
4. **Extract** — Apply MVI to approved items, preview extraction (APPROVAL)
5. **Cleanup** *(APPROVAL)* — Archive/delete/keep source files
6. **Report** — Summary: items harvested, workspace cleaned, navigation updated

**Usage**: `/context harvest [directory|file]`

---

## Common Patterns

### Approval Gates
ALL operations with approval stages MUST: show preview → wait for explicit input → provide options (yes/no/edit/preview/dry-run) → never proceed without confirmation

### Backups
File-modifying operations MUST: create backup before changes → store in `.tmp/backup/{operation}-{topic}-{timestamp}/` → report location

---

## Related

- harvest.md — Harvest workflow details
- extract.md — Extract workflow details
- organize.md — Organize workflow details
- update.md — Update workflow details
- error.md — Error workflow details
