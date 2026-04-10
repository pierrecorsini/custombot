<!-- Context: core/harvest | Priority: medium | Version: 1.0 | Updated: 2026-02-15 -->

# Context Harvest Operation

**Purpose**: Extract knowledge from AI summaries → permanent context, then clean workspace

**Last Updated**: 2026-01-06

---

## Core Concept

AI agents create summary files (OVERVIEW.md, SESSION-*.md) that contain valuable knowledge but clutter the workspace. Harvest extracts the knowledge into permanent context, then archives/deletes the summaries.

---

## Auto-Detection Patterns

**Filenames**: `*OVERVIEW.md`, `*SUMMARY.md`, `SESSION-*.md`, `CONTEXT-*.md`, `*NOTES.md`

**Locations**: Files in `.tmp/`, files with "Summary"/"Overview"/"Session" in title, root files >2KB

---

## 6-Stage Workflow

### Stage 1: Scan
Search for auto-detection patterns. List files with sizes, sorted by modification date.

### Stage 2: Analyze
Categorize content by function:

| Content Type | Target Folder | Signal |
|---|---|---|
| Design decisions | `concepts/` | "We decided to...", "Architecture" |
| Solutions/patterns | `examples/` | Code snippets, "Here's how..." |
| Workflows | `guides/` | Numbered steps, "How to..." |
| Errors encountered | `errors/` | "Fixed issue", "Gotcha" |
| Reference data | `lookup/` | Tables, lists, paths |

### Stage 3: Approve *(CRITICAL)*
Present letter-based selection UI:
- `A B C` — Approve specific items
- `all` — Approve all ✓ items
- `none` — Skip harvesting, delete files anyway
- `cancel` — Keep files, don't harvest

**Rule**: NEVER auto-harvest without user confirmation.

### Stage 4: Extract
Apply MVI to all approved content: core concept (1-3 sentences), key points (3-5), minimal example (<10 lines), reference link. Show extraction preview → get approval → write files + cross-references + update navigation.

### Stage 5: Cleanup *(APPROVAL)*
Options: Archive to `.tmp/archive/harvested/{date}/` (default) | Delete permanently | Keep in place.

**Rule**: ONLY cleanup files that had content successfully harvested.

### Stage 6: Report
Summary: items harvested, files created/updated, navigation maps updated, disk space freed.

---

## Smart Content Detection

### ✅ Extract
- Design decisions, patterns that worked, errors + solutions
- API changes, performance findings, core concepts

### ❌ Skip
- Planning discussion, conversational notes, duplicate info
- TODO lists (→ task system), timestamps, session metadata

---

## Safety Features

1. **Approval gate** — Never auto-delete without confirmation
2. **Archive by default** — Move to `.tmp/archive/`, not permanent delete
3. **Validation** — Check file sizes, structure before committing
4. **Rollback** — Can restore from archive

---

## Usage

```bash
/context harvest                    # Scan entire workspace
/context harvest .tmp/              # Scan specific directory
/context harvest OVERVIEW.md        # Harvest specific file
```

---

## Related

- compact.md — How to minimize extracted content
- mvi.md — What to extract
- structure.md — Where files go
