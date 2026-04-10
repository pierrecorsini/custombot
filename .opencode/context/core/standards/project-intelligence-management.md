<!-- Context: standards/intelligence-mgmt | Priority: high | Version: 1.0 | Updated: 2025-01-12 -->

# Project Intelligence Management

> **What**: How to manage project intelligence files and folders.
> **When**: Use this guide when adding, updating, or removing intelligence files.
> **Related**: See `project-intelligence.md` for what and why.

## Quick Reference

| Action | Do This |
|--------|---------|
| Update existing file | Edit + bump frontmatter version |
| Add new file | Create `.md` + add to navigation.md |
| Add subfolder | Create folder + `navigation.md` + update parent nav |
| Remove file | Rename `.deprecated.md` + archive, don't delete |

---

## Update Existing Files

**Triggers**: Business changes → `business-domain.md`, new decision → `decisions-log.md`, new issues → `living-notes.md`, stack changes → `technical-domain.md`

**Process**: Edit file → Update frontmatter version/date → Keep under 200 lines → Commit

---

## Add New Files

**When**: New domain area, existing file exceeds 200 lines, or specialized context needs separation.

**Naming**: Kebab-case, descriptive (`user-research.md`, `api-docs.md`)

**Template**:
```html
<!-- Context: project-intelligence/{filename} | Priority: {high|medium} | Version: 1.0 | Updated: {YYYY-MM-DD} -->

# File Title
> One-line purpose statement

## Quick Reference
- **Purpose**: [What this covers]
- **Update When**: [Triggers]
- **Related Files**: [Links]

## Content
[Follow patterns from existing files]
```

**Process**: Create file → Add frontmatter → Follow existing patterns → Keep <200 lines → Add to `navigation.md`

---

## Create Subfolders

**When**: 5+ related files need grouping, or subdomain warrants separation.

**Structure**:
```
project-intelligence/
├── navigation.md
├── [new-subfolder]/
│   ├── navigation.md       # Required
│   ├── file-1.md
│   └── file-2.md
```

**Rule**: Every subfolder MUST have `navigation.md`. Max depth: 2 levels.

---

## Remove/Deprecate Files

1. Rename: `filename.md` → `filename.deprecated.md`
2. Add frontmatter: `<!-- DEPRECATED: {YYYY-MM-DD} - {Reason} -->` and `<!-- REPLACED BY: {new-file.md} -->`
3. Add banner: > ⚠️ **DEPRECATED**: See `new-file.md`
4. Mark as deprecated in `navigation.md`

**Never Delete** decision history or lessons learned — archive instead.

---

## Version Tracking

Frontmatter: `<!-- Context: {category} | Priority: {level} | Version: {MAJOR.MINOR} | Updated: {YYYY-MM-DD} -->`
- New file = 1.0, content update = MINOR, structure change = MAJOR, typo fix = PATCH

---

## Quality Standards

- Files <200 lines, 3-7 sections per file
- Required: frontmatter, Quick Reference, related files section
- ❌ Mix concerns, exceed 200 lines, delete files, skip frontmatter, duplicate info
- ✅ Focused and scannable, archive deprecated content, use frontmatter consistently

---

## Compact Checklist

- **Add**: [ ] Naming convention [ ] Frontmatter [ ] Quick Reference [ ] <200 lines [ ] In navigation.md [ ] Version 1.0
- **Update**: [ ] Targeted changes [ ] Version/date bumped [ ] <200 lines [ ] Nav updated
- **Subfolder**: [ ] 5+ files warranted [ ] Kebab-case name [ ] Has navigation.md [ ] In parent nav
- **Deprecate**: [ ] Renamed `.deprecated.md` [ ] Deprecation frontmatter [ ] Banner added [ ] Nav marked [ ] Replacement documented

---

## Related Files

- **Standard**: `project-intelligence.md`
- **Project Intelligence**: `../../project-intelligence/navigation.md`
- **Context System**: `../context-system.md`
