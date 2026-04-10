<!-- Context: core/context-system | Priority: critical | Version: 1.0 | Updated: 2026-02-15 -->

# Context System

**Purpose**: Minimal, concern-based knowledge organization for AI agents

---

## Core Principles

### 1. Minimal Viable Information (MVI)
Extract only core concepts (1-3 sentences), key points (3-5 bullets), minimal example, and reference link.
**Goal**: Scannable in <30 seconds. Reference full docs, don't duplicate them.

### 2. Concern-Based Structure
Organize by **what you're doing** (concern), then by **how** (approach/tech):

- **Pattern A: Function-Based** (repository-specific): `category/{concepts,examples,guides,lookup,errors}/`
- **Pattern B: Concern-Based** (multi-tech): `category/{concern}/{approach-or-tech}/`

Examples: `development/backend/api-patterns/`, `development/frontend/react/`

### 3. Token-Efficient Navigation
Every category has `navigation.md`: ASCII tree (~50 tokens) + Quick routes table (~100 tokens) + By-concern sections (~50 tokens). Target: ~200-300 tokens per nav file.

### 4. Specialized Navigation
For cross-cutting concerns: `ui-navigation.md`, `backend-navigation.md`, `fullstack-navigation.md`

### 5. Self-Describing Filenames
❌ `code.md` → ✅ `code-quality.md` | ❌ `tests.md` → ✅ `test-coverage.md`

### 6. Knowledge Harvesting
Extract valuable context from AI summaries, then delete originals. Workspace stays clean.

### 7. Technology Context Organization

| Type | Location | Examples |
|------|----------|----------|
| Full-stack frameworks | `development/frameworks/{tech}/` | Next.js, Tanstack Start |
| Specialized domains | `development/{concern}/{tech}/` | AI, Data |
| Layer-specific | `development/{frontend\|backend}/{tech}/` | React, Node.js |

**Decision**: Full-stack? → `frameworks/` → Specialized? → `{domain}/` → Layer? → `{frontend|backend}/`

---

## Organizing Principles

| Location | Scope | Content |
|----------|-------|---------|
| `core/standards/` | Universal (all projects) | Code quality, testing, docs, security |
| `development/principles/` | Development-specific | Clean code, API design, error handling |
| `development/data/` | Data patterns | SQL, NoSQL, ORM patterns |

---

## Operations

| Command | Purpose | Key Steps |
|---------|---------|-----------|
| `/context harvest` | Summaries → permanent context | Scan patterns → Categorize (concepts/examples/guides/errors/lookup) → Approve → Extract → Archive |
| `/context extract` | From docs/code/URLs | Read → Extract concepts → Find examples → Identify workflows → Build lookups → Capture errors |
| `/context organize` | Restructure files | Scan → Determine pattern → Create dirs → Move files → Update nav → Fix refs |
| `/context update` | When APIs change | Identify changes → Find affected files → Update content → Add migration notes → Validate |
| `/context create` | New category | Function-based structure with navigation.md |
| `/context error` | Add recurring error | Search existing → Deduplicate → Add/update → Cross-reference |
| `/context compact` | Minimize to MVI | Identify verbose sections → Compress to MVI format |
| `/context validate` | Check integrity | References, sizes, structure |

---

## File Naming

- **Navigation**: `navigation.md` (main), `{domain}-navigation.md` (specialized)
- **Content**: Descriptive kebab-case (`code-quality.md`, `jwt-patterns.md`, `scroll-linked-animations.md`)

---

## Success Criteria

✅ **Minimal** - Core info only, <200 lines per file
✅ **Navigable** - navigation.md at every level
✅ **Organized** - Appropriate pattern (function or concern-based)
✅ **Token-efficient** - Nav files ~200-300 tokens
✅ **Self-describing** - Filenames tell you what's inside
✅ **Referenceable** - Links to full docs

---

## Quick Commands

```bash
/context                      # Quick scan, suggest actions
/context harvest              # Summaries → permanent context
/context extract {source}     # From docs/code/URLs
/context organize {category}  # Restructure → function folders
/context update {what}        # When APIs/frameworks change
/context create {category}    # New context category
/context error {error}        # Add error to knowledge base
/context compact {file}       # Minimize to MVI format
/context map [category]       # View context structure
/context validate             # Check integrity
```

**All operations show preview before asking for approval.**
