<!-- Context: core/structure | Priority: critical | Version: 1.0 | Updated: 2026-02-15 -->

# Context Structure

**Purpose**: Function-based folder organization for easy discovery

**Last Updated**: 2026-01-06

---

## Required Structure

```
.opencode/context/{category}/
├── navigation.md          # Navigation map (REQUIRED)
├── concepts/              # What it is
├── examples/              # Working code
├── guides/                # How to do it
├── lookup/                # Quick reference
└── errors/                # Common issues
```

**Rule**: ALWAYS organize by function (what info does), not by topic.

---

## Folder Purposes

| Folder | Purpose | Contains | Rule |
|--------|---------|----------|------|
| `concepts/` | Core ideas, "what is it?" | Design decisions, patterns, principles | 1-3 sentences + 3-5 bullets |
| `examples/` | Working code | Minimal snippets, patterns in action | <30 lines of code, functional |
| `guides/` | Step-by-step workflows | Procedures, setup, migrations | Actionable steps, not theory |
| `lookup/` | Quick reference | Commands, paths, API endpoints | Table/list format (scannable) |
| `errors/` | Issues & fixes | Error messages, pitfalls, edge cases | Group by framework/topic |

---

## Categorization Rules

| Question | Folder |
|----------|--------|
| Does it explain **what** something is? | `concepts/` |
| Does it show **working code**? | `examples/` |
| Does it explain **how to do** something? | `guides/` |
| Is it **quick reference** data? | `lookup/` |
| Does it document an **error/issue**? | `errors/` |

---

## navigation.md Requirement

Every category MUST have `navigation.md` at root with:
1. Purpose (1-2 sentences)
2. Navigation tables for each function folder
3. Priority levels (critical/high/medium/low)
4. Loading strategy (what to load for common tasks)

---

## Anti-Patterns

❌ **Flat structure** — Files in root without folders. Can't tell if `authentication.md` is a concept or guide.

✅ **Function-based** — Files in `concepts/authentication.md` → instantly know purpose by location.

---

## Validation

- [ ] All categories have navigation.md?
- [ ] Files in function folders (not flat)?
- [ ] Priority levels assigned?
- [ ] Loading strategy documented?

---

## Related

- mvi.md — What to extract
- templates.md — File formats
- creation.md — How to create files
