<!-- Context: openagents-repo/guides | Priority: high | Version: 2.0 | Updated: 2026-01-29 -->

# Guide: External Libraries Workflow

**Golden Rule**: NEVER rely on training data for external libraries. ALWAYS fetch current docs via ExternalScout.

---

## When to Use ExternalScout (MANDATORY)

- Adding agents/skills that depend on external packages
- First-time package setup
- Dependency errors
- Version upgrades

---

## Workflow

### Step 1: Detect External Package
Triggers: library name mentioned, imports in code, package.json dependencies, build errors

### Step 2: Check Install Scripts (First-Time Only)
```bash
ls scripts/install/ scripts/setup/ setup.sh install.sh
grep -r "postinstall\|preinstall" package.json
```

### Step 3: Fetch Current Documentation (MANDATORY)
```javascript
task(
  subagent_type="ExternalScout",
  description="Fetch {library} documentation",
  prompt="Fetch current docs for {library}: schema patterns, integration, setup, migration"
)
```

### Step 4: Implement with Fresh Knowledge
Use the fetched docs for current APIs, patterns, and version-specific features.

---

## Common Packages

| Package | Use Case |
|---------|----------|
| Drizzle ORM | Database schemas & queries |
| Better Auth | Authentication |
| Next.js | Full-stack web framework |
| TanStack Query | Server state |
| Zod | Schema validation |
| Tailwind CSS | Styling |
| Shadcn/ui | UI components |
| Vitest | Testing |

---

## Why Training Data Fails

```
Training data (2023): Next.js 13 uses pages/ directory
Current (2025): Next.js 15 uses app/ directory (App Router)
→ Training data = broken code ❌
→ ExternalScout = working code ✅
```

## Checklist

- [ ] Identified all external packages
- [ ] Checked install scripts
- [ ] Fetched current docs via ExternalScout
- [ ] Documented dependencies in metadata
- [ ] Tested implementation

## Related

- `guides/adding-agent-basics.md` — Creating agents
- `guides/debugging.md` — Troubleshooting
