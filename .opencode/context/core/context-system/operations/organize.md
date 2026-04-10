<!-- Context: core/organize | Priority: medium | Version: 1.0 | Updated: 2026-02-15 -->

# Organize Operation

**Purpose**: Restructure flat context files into function-based folder structure

**Last Updated**: 2026-01-06

---

## 8-Stage Workflow

### Stage 1: Scan
Detect all files and current structure (flat vs organized).

### Stage 2: Categorize
Map each file to function folder using categorization rules (see structure.md). Flag ambiguous files.

### Stage 3: Resolve Conflicts *(APPROVAL)*
- **Ambiguous files** (fit multiple categories): Split / Keep in primary / User decides
- **Duplicate targets** (file already exists): Merge / Rename (-v2) / Skip
- Present with letter-based selection (e.g., `A J M` or `auto`)

### Stage 4: Preview *(APPROVAL)*
Show: CREATE dirs, MOVE files, SPLIT ambiguous, MERGE conflicts, UPDATE references count.
Options: dry-run (simulate without executing) / approve / show-diff.

### Stage 5: Backup
Save to `.tmp/backup/organize-{category}-{timestamp}/` for rollback.

### Stage 6: Execute
Create function folders. Move files. Split/merge as approved.

### Stage 7: Update
Update navigation.md with tables. Fix all internal references. Validate links.

### Stage 8: Report
Summary: files organized, folders created, files split, references fixed, backup location.

---

## Conflict Resolution

| Conflict Type | Options |
|---|---|
| Ambiguous (fits multiple folders) | **Split** into separate files (recommended) / Keep in primary / User choice |
| Duplicate target (file exists) | **Merge** into existing / **Rename** (-v2) / **Skip** |
| Auto-resolution | Agent suggests based on file size, content analysis, existing structure |

---

## Usage

```bash
/context organize development/           # Organize flat directory
/context organize development/ --dry-run # Preview without changes
```

---

## Success Criteria

- [ ] All files in function folders (not flat)?
- [ ] Ambiguous files resolved?
- [ ] Conflicts handled?
- [ ] navigation.md created/updated?
- [ ] All references fixed?
- [ ] Backup created?

---

## Related

- standards/structure.md — Folder organization rules
- guides/workflows.md — Full workflow reference
