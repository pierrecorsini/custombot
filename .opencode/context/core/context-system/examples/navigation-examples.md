<!-- Context: core/navigation-examples | Priority: high | Version: 1.0 | Updated: 2026-02-15 -->

# Examples: Navigation Files

**Purpose**: Minimal navigation file examples

**Last Updated**: 2026-01-08

---

## Example 1: Function-Based Category (~250 tokens)

```markdown
# OpenAgents Navigation
**Purpose**: Navigate OpenAgents repository context

---

## Structure
openagents-repo/
├── navigation.md
├── core-concepts/  (agent-architecture, registry-system)
├── guides/         (adding-agent, debugging-issues)
├── lookup/         (commands)
└── errors/         (tool-permission-errors)

## Quick Routes
| Task | Path |
|------|------|
| **Add agent** | `guides/adding-agent.md` |
| **Fix error** | `errors/tool-permission-errors.md` |
```

---

## Example 2: Minimal Category (~150 tokens)

```markdown
# Content Navigation
**Purpose**: Copywriting and content creation

## Quick Routes
| Task | Path |
|------|------|
| **Write copy** | `copywriting-frameworks.md` |
| **Set tone** | `tone-voice.md` |
```

---

## Example 3: Cross-Cutting (~270 tokens)

```markdown
# UI Development Navigation
**Scope**: Frontend code + visual design

## Quick Routes
| Task | Path |
|------|------|
| **React** | `frontend/react/hooks-patterns.md` |
| **Animations** | `../../ui/web/animation-basics.md` |

## By Concern
**Code** → `development/frontend/` | **Design** → `ui/web/`
```

---

## Key Takeaways

1. **200-300 tokens max** — ASCII tree + routes table = essential elements
2. **Point, don't explain** — Reference files, never contain them
3. ❌ Avoid: verbose paragraphs, flat lists, duplicated file contents

---

## Related

- `../guides/navigation-design.md` — How to create navigation files
- `../standards/templates.md` — Navigation template format
