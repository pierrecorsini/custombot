<!-- Context: core/extract | Priority: medium | Version: 1.0 | Updated: 2026-02-15 -->

# Extract Operation

**Purpose**: Extract context from docs, code, or URLs into organized context files

**Last Updated**: 2026-01-06

---

## 7-Stage Workflow

### Stage 1: Read Source
Read URL/file/codebase. Analyze for extractable items.

### Stage 2: Analyze & Categorize
Map content to function folders: concepts/, examples/, guides/, lookup/, errors/. Output: list of extractable items with previews.

### Stage 3: Select Category *(APPROVAL)*
User chooses target category (`development/`, `core/`, or new) and selects items via letter IDs or `all`.

### Stage 4: Preview *(APPROVAL)*
Show: files to CREATE (with line counts), files to ADD TO (existing), CONFLICTS (overwrite/skip/merge options), NAVIGATION updates. Options: preview content / approve / edit.

### Stage 5: Create
Apply MVI format (1-3 sentences, 3-5 key points, minimal example, <200 lines). Write to correct function folders. Add cross-references.

### Stage 6: Update Navigation
Update navigation.md with new files, priorities, cross-references.

### Stage 7: Report
Summary: items extracted, files created, navigation updated.

---

## Usage

```bash
/context extract from https://react.dev/hooks   # From URL
/context extract from docs/api.md                # From local docs
/context extract from src/utils/                 # From codebase
```

---

## Success Criteria

- [ ] All files <200 lines?
- [ ] MVI format applied?
- [ ] Files in correct function folders?
- [ ] navigation.md updated?
- [ ] Cross-references added?
- [ ] User approved before creation?

---

## Related

- standards/mvi.md — What to extract
- guides/compact.md — How to minimize
- guides/workflows.md — Full workflow reference
