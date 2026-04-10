<!-- Context: core/templates | Priority: critical | Version: 1.0 | Updated: 2026-02-15 -->

# Context File Templates

**Purpose**: Standard formats for all context file types

**Last Updated**: 2026-01-06

---

## Frontmatter (ALL files)

`<!-- Context: {category}/{type} | Priority: {critical|high|medium|low} | Version: 1.0 | Updated: YYYY-MM-DD -->`

---

## Template Quick Reference

| Type | Max | Sections |
|------|-----|----------|
| Concept | 100 | Purpose, Core Idea (1-3 sentences), Key Points (3-5), Example (<10 lines), Reference, Related |
| Example | 80 | Purpose, Use Case, Code (10-30 lines), Explanation, Related |
| Guide | 150 | Purpose, Prerequisites, Steps (4-7), Verification, Troubleshooting, Related |
| Lookup | 100 | Purpose, Tables/Lists, Commands, Related |
| Error | 150 | Per-error: Symptom, Cause, Solution, Code (❌/✅), Prevention, Frequency, Reference |
| Navigation | 100 | Purpose, ASCII tree, Quick Routes table, By-section |

---

## Concept Skeleton

`# Concept: {Name}` → **Purpose** → **Core Idea** (1-3 sentences) → **Key Points** (3-5 bullets) → **Quick Example** (<10 lines code) → **📂 Codebase References** → **Related**

## Example Skeleton

`# Example: {What}` → **Purpose** → **Use Case** (2-3 sentences) → **Code** (10-30 lines) → **Explanation** (numbered steps) → **Related**

## Guide Skeleton

`# Guide: {Action}` → **Purpose** → **Prerequisites** → **Steps** (4-7, each with command + expected result) → **Verification** command → **Troubleshooting** table → **Related**

## Lookup Skeleton

`# Lookup: {Type}` → **Purpose** → Table(s) with columns (Item | Value | Desc) → **Commands** section → **Related**

## Error Skeleton

`# Errors: {Framework}` → Repeat per error: **Symptom** (error msg) → **Cause** (1-2 sentences) → **Solution** (steps) → **Code** (❌/✅) → **Prevention** → **Frequency** → **Reference** → **Related**

---

## Required in ALL Templates

1. Title with type prefix (`# Concept:`, `# Example:`, etc.)
2. **Purpose** (1 sentence)
3. **Related** section (cross-references)

---

## Related

- creation.md — When to use each template
- mvi.md — How to fill templates
