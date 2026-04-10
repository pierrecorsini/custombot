<!-- Context: development/frontend/when-to-delegate | Priority: high | Version: 1.1 | Updated: 2026-04-05 -->
# When to Delegate to Frontend Specialist

## Quick Reference

**Delegate**: UI/UX design, design systems, complex responsive layouts, animations, multi-stage design iterations

**Handle directly**: Simple HTML/CSS edits, single component updates, bug fixes, content changes

---

## Decision Matrix

### ✅ DELEGATE to Frontend-Specialist

| Scenario | Why Delegate |
|----------|-------------|
| New UI design from scratch | Staged workflow (layout → theme → animation → implement) |
| Design system work | Needs ContextScout + ExternalScout for standards and UI libs |
| Complex responsive layouts | Mobile-first approach across all breakpoints |
| Animation implementation | Performance optimization (<400ms), micro-interactions |
| Multi-stage design iterations | Versioning via design_iterations/ folder |
| Theme creation | OKLCH colors, CSS custom properties |
| Component library integration | ExternalScout for current docs (Flowbite, Radix, etc.) |

### ⚠️ HANDLE DIRECTLY

| Scenario | Why Direct |
|----------|-----------|
| Simple HTML edits | Single file, straightforward |
| Minor CSS tweaks | Small styling adjustment |
| Bug fixes | Fix existing code, not new design |
| Content updates | Text/image/data changes only |
| Single component updates | Modify one existing component |

---

## Delegation Checklist

Before delegating, verify:

- [ ] Task is UI/design focused (not backend, logic, or data)
- [ ] Task requires design expertise (layout, theme, animations)
- [ ] Task benefits from staged workflow
- [ ] Task needs context discovery (design systems, UI libraries)
- [ ] User has approved the approach — **never delegate before approval**

---

## How to Delegate

1. **Propose approach** — present a plan to the user with task, approach, why, context needed
2. **Get approval** — wait for explicit user sign-off
3. **Delegate with context** — specify context files to load and clear requirements

```javascript
task(
  subagent_type="frontend-specialist",
  description="Create landing page design",
  prompt="Context to load:
  - .opencode/context/ui/web/design-systems.md
  - .opencode/context/ui/web/ui-styling-standards.md
  - .opencode/context/ui/web/animation-basics.md
  
  Task: Create a landing page with hero, features grid, CTA.
  Requirements: Tailwind + Flowbite, mobile-first, animations <400ms.
  Follow staged workflow. Request approval between each stage."
)
```

---

## Red Flags (Don't Delegate)

- ❌ Quick fix or single-line change
- ❌ Backend/logic focused task
- ❌ Content/text update only
- ❌ Testing/validation task (wrong subagent)
- ❌ Code review task (wrong subagent)

## Green Flags (Delegate)

- ✅ New UI design from scratch
- ✅ Design system implementation
- ✅ Complex responsive layouts
- ✅ Animation work
- ✅ UI library integration
- ✅ Multi-stage design iterations

---

## Frontend-Specialist Capabilities

**Does well**: Complete UI designs, design systems (Tailwind/Shadcn/Flowbite), responsive layouts, animations/micro-interactions, OKLCH themes, staged workflow with versioning

**Doesn't do**: Backend logic, database queries, testing, code review, simple HTML edits, content updates

---

## Context Files Used

- `ui/web/design-systems.md` — Theme templates, color systems
- `ui/web/ui-styling-standards.md` — Tailwind, Flowbite, responsive design
- `ui/web/animation-basics.md` — Animation syntax, micro-interactions
- `ui/web/design-assets.md` — Images, icons, fonts

---

## Related Context

- **Frontend Specialist Agent** → `../../../agent/subagents/development/frontend-specialist.md`
- **Design Systems** → `../../ui/web/design-systems.md`
- **UI Styling Standards** → `../../ui/web/ui-styling-standards.md`
- **Animation Basics** → `../../ui/web/animation-basics.md`
- **React Patterns** → `react/react-patterns.md`
