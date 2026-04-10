<!-- Context: core/update | Priority: medium | Version: 1.0 | Updated: 2026-02-15 -->

# Update Operation

**Purpose**: Update context when APIs, frameworks, or contracts change

**Last Updated**: 2026-01-06

---

## 8-Stage Workflow

### Stage 1: Identify Changes *(APPROVAL)*
User selects change types: [A] API changes, [B] Deprecations, [C] New features, [D] Breaking changes, [E] Other. Then provides specific details for each.

### Stage 2: Find Affected Files
Grep for topic references across all context. Show impact: file paths, reference counts, line counts.

### Stage 3: Preview Changes *(APPROVAL)*
Show line-by-line diff for each file (with `-` removed / `+` added markers). Options: preview next file / show all / approve / edit (line-by-line approval mode).

### Stage 4: Backup
Save to `.tmp/backup/update-{topic}-{timestamp}/` for rollback.

### Stage 5: Update Files
Apply approved changes. Maintain MVI format (<200 lines). Update "Last Updated" dates.

### Stage 6: Add Migration Notes
Append to `errors/{topic}-errors.md`:
```markdown
## Migration: {Old} → {New}
**Breaking Changes**: - Change 1
**Migration Steps**: 1. Step 1
**Reference**: [Changelog URL]
```

### Stage 7: Validate
Check: internal references work, no broken links, all files <200 lines, MVI format maintained.

### Stage 8: Report
Summary: files updated, references modified, migration notes added, backup location.

---

## Change Types Reference

| Type | Signals | Action |
|------|---------|--------|
| API Changes | Signatures, params, return types | Update code examples + lookup tables |
| Deprecations | Features marked deprecated | Add warnings, link to replacements |
| New Features | New capabilities/APIs | Add concept + example files |
| Breaking Changes | Incompatible changes | Add migration notes to errors/ |

---

## Usage

```bash
/context update for Next.js 15
/context update for Stripe API v2024
/context update for Tailwind CSS v4
```

---

## Success Criteria

- [ ] User described changes?
- [ ] All affected files found?
- [ ] Diff preview shown and approved?
- [ ] Backup created?
- [ ] Migration notes added (if breaking)?
- [ ] All files still <200 lines?

---

## Related

- guides/workflows.md — Full workflow reference
- operations/error.md — Adding migration notes
