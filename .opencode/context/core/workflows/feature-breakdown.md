<!-- Context: workflows/task-breakdown | Priority: high | Version: 2.0 | Updated: 2025-01-21 -->

# Task Breakdown Guidelines

## Quick Reference

**When to Use**: 4+ files, >60 min effort, complex dependencies, multi-step coordination

**Process**: Scope → Phases → Small Tasks (1-2h) → Dependencies → Estimates

---

## When to Use

- Task involves 4+ files
- Estimated effort >60 minutes
- Complex dependencies exist
- Multi-step coordination needed
- User requests task breakdown

## Breakdown Process

1. **Scope**: Understand full requirement, components, end goal, constraints
2. **Phases**: Identify logical groupings, sequential vs parallel work
3. **Tasks**: Break into 1-2 hour max items, clear and independently completable
4. **Dependencies**: What blocks what, critical path, parallel opportunities
5. **Estimate**: Realistic times including testing, buffer for unknowns

## Breakdown Template

```markdown
# Task Breakdown: {Task Name}

## Overview
{1-2 sentence description}

## Prerequisites
- [ ] {Prerequisite}

## Tasks

### Phase 1: {Phase Name}
**Goal:** {What this phase accomplishes}

- [ ] **Task 1.1:** {Description}
  - **Files:** {files} | **Estimate:** {time} | **Dependencies:** {none/task X}
  - **Verification:** {how to verify}

## Testing Strategy
- [ ] Unit tests for {component}
- [ ] Integration tests for {flow}

## Total Estimate
**Time:** {X} hours | **Complexity:** {Low/Medium/High}
```

## Minimal Example

```markdown
# Task Breakdown: Add User Profile Page

## Phase 1: Data Layer
- [ ] 1.1: Create user profile schema → 30min → verify: DB migration runs
- [ ] 1.2: Add profile API endpoint → 1h → deps: 1.1 → verify: API returns data

## Phase 2: UI Layer
- [ ] 2.1: Build profile component → 1h → deps: Phase 1 → verify: Renders mock data
- [ ] 2.2: Add edit functionality → 1h → deps: 2.1 → verify: Can save changes

## Total: 3.5 hours | Complexity: Low
```

---

## Best Practices

- **Small tasks**: 1-2 hours max; if larger, break further
- **Clear dependencies**: Explicitly state prerequisites, identify parallel work
- **Include verification**: How do you know it's done? What should work when complete?
- **Realistic estimates**: Include testing time, account for unknowns, overestimate rather than underestimate
- **Logical grouping**: Organize by feature/component, keep phases cohesive

---

## Common Patterns

- **Database-First**: Schema → Migrations → Models → Business logic → API → Tests
- **Feature-First**: Requirements → Interface → Core logic → Error handling → Tests → Docs
- **Refactoring**: Add tests → Refactor small section → Verify tests → Repeat → Cleanup

---

## Quick Checklist

- [ ] All requirements captured
- [ ] Tasks are 1-2 hours each
- [ ] Dependencies identified
- [ ] Estimates realistic (include testing)
- [ ] Verification criteria clear
