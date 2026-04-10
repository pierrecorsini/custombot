<!-- Context: core/error | Priority: medium | Version: 1.0 | Updated: 2026-02-15 -->

# Error Operation

**Purpose**: Add recurring errors to knowledge base with deduplication

**Last Updated**: 2026-01-06

---

## 6-Stage Workflow

### Stage 1: Search Existing
Search error message across all `errors/` files. Find similar (fuzzy match) and related (same category) errors.

### Stage 2: Check Duplication *(APPROVAL)*
Present options:
- **[A] Add new** — Create new error entry
- **[B] Update existing** — Add examples to existing entry
- **[C] Skip** — Already covered

Also select target file: existing framework file or create new.

### Stage 3: Preview *(APPROVAL)*
Show full before/after with diff markers (`← NEW`, `← UPDATED`). Display file size impact. Allow edit mode.

### Stage 4: Add/Update
Write error entry following template:

```markdown
## Error: {Name}
**Symptom**: {error message}
**Cause**: [1-2 sentences]
**Solution**: [Steps]
**Code**: ❌ Before / ✅ After
**Prevention**: [How to avoid]
**Frequency**: common/occasional/rare
**Reference**: [Link]
```

### Stage 5: Update Navigation
Add cross-references to related errors. Update navigation.md if new file created.

### Stage 6: Report
Summary: error added/updated, cross-references, file size.

---

## Deduplication Strategy

| Type | Pattern | Action |
|------|---------|--------|
| Similar | Same root cause, different manifestation | **Update existing** to include new examples |
| Related | Different causes, same category | **Cross-reference** between errors |
| Duplicate | Exact same error already documented | **Skip** (already covered) |
| New | Unique error not yet documented | **Add as new** entry |

---

## Error Grouping

Group errors by framework/topic in a single file:
- `react-errors.md` — All React errors
- `nextjs-errors.md` — All Next.js errors
- `auth-errors.md` — All authentication errors

**Don't**: Create one file per error (too granular)

---

## Usage

```bash
/context error for "hooks can only be called inside components"
/context error for "Cannot read property 'map' of undefined"
```

---

## Related

- standards/templates.md — Error template format
- guides/workflows.md — Full workflow reference
