<!-- Context: project-intelligence/nav | Priority: high | Version: 3.0 | Updated: 2026-04-06 -->

# Project Intelligence

> Start here. Function-based context for quick project understanding.

## Structure

```
project-intelligence/
├── navigation.md                    # ← You are here
├── concepts/                        # Core understanding (what & why)
│   ├── architecture.md              # Native Python architecture, patterns, constraints
│   ├── business-domain.md           # Business context, users, value proposition
│   └── business-tech-bridge.md      # Business needs → technical solutions mapping
├── guides/                          # How-to knowledge (how)
│   ├── dev-environment.md           # Setup, run, develop, deploy
│   └── log-diagnostics.md           # Log file diagnosis and troubleshooting
├── lookup/                          # Quick reference tables (what's what)
│   ├── tech-stack.md                # All technologies, versions, roles
│   ├── project-structure.md         # Directory tree and key locations
│   ├── completed-sessions.md        # Development session history
│   └── decisions-log.md             # Major decisions with rationale
└── errors/                          # Problems and fixes (what's broken)
    ├── known-issues.md              # Technical debt, open questions, active issues
    └── bug-fixes.md                 # Past bugs fixed and patterns to watch
```

## Quick Routes

| What You Need | File | Key Content |
|---------------|------|-------------|
| **Understand the architecture** | `concepts/architecture.md` | ctypes pattern, integrations, constraints |
| **Why this project exists** | `concepts/business-domain.md` | Problem, users, value |
| **How biz maps to tech** | `concepts/business-tech-bridge.md` | Business-technical trade-offs |
| **Set up dev environment** | `guides/dev-environment.md` | Install, run, test, deploy |
| **Diagnose a problem** | `guides/log-diagnostics.md` | Log location, search patterns |
| **What technologies?** | `lookup/tech-stack.md` | Stack table, supporting modules |
| **Where is X in the code?** | `lookup/project-structure.md` | Directory tree, key dirs |
| **What was built?** | `lookup/completed-sessions.md` | Session history, pending work |
| **Why was X decided?** | `lookup/decisions-log.md` | Decisions with alternatives |
| **What's broken?** | `errors/known-issues.md` | Debt, open questions, issues |
| **Past bug fixes** | `errors/bug-fixes.md` | Fixed bugs, diagnostic patterns |

## Onboarding Path

1. `navigation.md` (this file) — orient yourself
2. `concepts/architecture.md` — understand the system
3. `lookup/tech-stack.md` — know the tools
4. `lookup/project-structure.md` — find your way around
5. `guides/dev-environment.md` — get running

## Integration

This folder is referenced from:
- `.opencode/context/core/standards/` — standards and patterns
- `.opencode/context/core/system/context-guide.md` — context loading

## Maintenance

- Update when business direction or architecture changes
- Document decisions as they're made in `lookup/decisions-log.md`
- Review `errors/known-issues.md` weekly
- Log bug fixes in `errors/bug-fixes.md`
- Archive resolved items from known-issues
