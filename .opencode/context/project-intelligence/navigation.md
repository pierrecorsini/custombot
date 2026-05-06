<!-- Context: project-intelligence/navigation | Priority: critical | Version: 2.1 | Updated: 2026-05-06 -->

# Project Intelligence

**Purpose**: Quick routes to project-specific context files

---

## Quick Routes

| File | Description | Priority |
|------|-------------|----------|
| `technical-domain.md` | Tech stack, architecture, code patterns, naming conventions | critical |

---

## Concepts

| File | Description | Priority |
|------|-------------|----------|
| `concepts/architecture.md` | Native Python architecture, integration points, resilience patterns | high |
| `concepts/business-domain.md` | Business context and value (template — needs filling) | high |
| `concepts/business-tech-bridge.md` | Business ↔ technical mapping (template — needs filling) | high |

## Guides

| File | Description | Priority |
|------|-------------|----------|
| `guides/cli-reference.md` | All CLI commands, options, flags, workflows | high |
| `guides/dev-environment.md` | Setup, run, develop, deploy | medium |
| `guides/log-diagnostics.md` | Log-based issue diagnosis patterns | medium |
| `guides/optimization-patterns.md` | Reusable micro-optimization patterns for hot paths | high |

## Lookup

| File | Description | Priority |
|------|-------------|----------|
| `lookup/skills-reference.md` | Built-in skills, skill contract, adding custom skills | high |
| `lookup/config-reference.md` | All config.json fields, types, defaults, LLM providers | high |
| `lookup/project-structure.md` | Directory layout and key locations | high |
| `lookup/tech-stack.md` | Technologies, versions, roles | high |
| `lookup/decisions-log.md` | Architectural decisions with context and rationale | high |
| `lookup/completed-sessions.md` | Session history and deliverables | medium |

## Errors

| File | Description | Priority |
|------|-------------|----------|
| `errors/known-issues.md` | Active tech debt, open issues, gotchas | high |
| `errors/bug-fixes.md` | Past bugs, fixes, patterns to watch | high |

---

## By Concern

**Architecture** → concepts/architecture.md + technical-domain.md
**Config** → lookup/config-reference.md + guides/cli-reference.md
**Skills** → lookup/skills-reference.md
**Debugging** → errors/known-issues.md + guides/log-diagnostics.md
**History** → lookup/completed-sessions.md + lookup/decisions-log.md + errors/bug-fixes.md

---

## Management

- Update patterns: `/add-context --update`
- View structure: `/context map project-intelligence`
- Validate: `/context validate`
