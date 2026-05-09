<!-- Context: project/navigation | Priority: high | Version: 2.6 | Updated: 2026-05-07 -->

# Project Context — custombot

> Feature-level documentation for the custombot WhatsApp AI assistant. Complements `project-intelligence/` which covers business/technical domain.

---

## Structure

```
.opencode/context/project/
├── navigation.md          ← This file
├── concepts/              # Core architecture concepts
├── errors/                # Bug fixes & error solutions
├── examples/              # Working code examples
├── guides/                # How-to guides
├── lookup/                # Quick reference tables
└── project-context.md     ← (archived to .tmp/archive/harvested/2026-04-06/)
```

---

## Quick Routes

| Task | Path |
|------|------|
| **Understand system architecture** | `concepts/architecture-overview.md` |
| **Understand lifecycle phases** | `concepts/lifecycle-state-machine.md` |
| **Understand the ReAct loop** | `concepts/react-loop.md` |
| **Understand message routing** | `concepts/routing-engine.md` |
| **Understand middleware pipeline** | `concepts/middleware-pipeline.md` |
| **Understand skill architecture** | `concepts/skills-system.md` |
| **Understand per-chat memory** | `concepts/per-chat-memory.md` |
| **Understand vector memory** | `concepts/vector-memory.md` |
| **Understand OTel tracing** | `concepts/otel-tracing.md` |
| **Stealth/anti-detection patterns** | `concepts/stealth-patterns.md` |
| **Crash recovery system** | `concepts/crash-recovery.md` |
| **Graceful shutdown** | `concepts/graceful-shutdown.md` |
| **Task scheduler** | `concepts/task-scheduler.md` |
| **Project & knowledge mgmt** | `concepts/project-knowledge.md` |
| **Planner / task tracking** | `concepts/planner.md` |
| **Web research skill** | `concepts/web-research.md` |
| **Rate limiting** | `concepts/rate-limiting.md` |
| **Monitoring & metrics** | `concepts/monitoring-metrics.md` |
| **Media output (TTS/PDF)** | `concepts/media-output.md` |
| **LLM error classification** | `concepts/llm-error-classification.md` |
| **Event bus** | `concepts/event-bus.md` |
| **Deduplication service** | `concepts/dedup-service.md` |
| **Security subsystem** | `concepts/security-subsystem.md` |
| **Non-critical error system** | `concepts/noncritical-errors.md` |
| **Design philosophy** | `concepts/design-philosophy.md` |
| **Step orchestrator pattern** | `concepts/step-orchestrator.md` |
| **Code review decisions (R11)** | `concepts/code-review-decisions.md` |
| **Bug fixes log** | `errors/bug-fixes.md` |
| **Known error gaps** | `errors/known-gaps.md` |
| **Security improvement gaps** | `errors/security-gaps.md` |
| **Implemented modules** | `lookup/implemented-modules.md` |
| **PLAN.md progress** | `lookup/plan-progress.md` |
| **Improvement roadmap** | `lookup/improvement-roadmap.md` |
| **OpenAI exceptions** | `lookup/openai-exceptions.md` |
| **Agent instruction templates** | `examples/agent-instruction-template.md` |
| **Create a Python skill** | `guides/skill-development.md` → `examples/python-skill.md` |
| **Create a Markdown skill** | `guides/skill-development.md` → `examples/markdown-skill.md` |
| **CLI commands** | `guides/cli-reference.md` |
| **OTel tracing setup** | `guides/otel-tracing.md` |
| **Find a built-in skill** | `lookup/built-in-skills.md` |
| **Workspace file layout** | `lookup/workspace-structure.md` |
| **Config.json fields** | `lookup/configuration.md` |

---

## By Folder

### concepts/ — Core Architecture (how things work)

| File | Topic | Lines |
|------|-------|-------|
| `architecture-overview.md` | System-wide architecture + component map | ~85 |
| `lifecycle-state-machine.md` | AppPhase state machine + startup sequence | ~85 |
| `react-loop.md` | ReAct message processing pipeline | ~81 |
| `routing-engine.md` | Priority-based message routing | ~91 |
| `middleware-pipeline.md` | Configurable middleware chain | ~60 |
| `skills-system.md` | Dual skill system architecture | ~83 |
| `per-chat-memory.md` | Isolated file-based memory per chat | ~79 |
| `vector-memory.md` | sqlite-vec semantic memory | ~75 |
| `otel-tracing.md` | OpenTelemetry distributed tracing | ~65 |
| `stealth-patterns.md` | Human-like timing for anti-detection | ~77 |
| `crash-recovery.md` | Persistent message queue + stale recovery | ~82 |
| `graceful-shutdown.md` | Ordered signal-based component teardown | ~65 |
| `task-scheduler.md` | Background scheduled LLM tasks | ~70 |
| `project-knowledge.md` | Projects, knowledge entries, graph recall | ~70 |
| `planner.md` | Task planning with dependency resolution | ~60 |
| `web-research.md` | Search + crawl skill | ~55 |
| `rate-limiting.md` | Sliding window per-chat + per-skill limits | ~60 |
| `monitoring-metrics.md` | LLM latency, tokens, queue, memory monitor | ~55 |
| `media-output.md` | TTS (edge-tts) + PDF + callback bridge | ~55 |
| `llm-error-classification.md` | LLM error classification + circuit breaker | ~85 |
| `event-bus.md` | Async typed pub/sub (7 events, singleton) | ~65 |
| `dedup-service.md` | Inbound + outbound dedup (xxhash + DB) | ~55 |
| `security-subsystem.md` | Defense-in-depth security layer (6 modules) | ~70 |
| `noncritical-errors.md` | 25+ categorized fire-and-forget error logging | ~55 |
| `step-orchestrator.md` | Generic dependency-ordered step execution | ~65 |
| `code-review-decisions.md` | Round 11 completed refactors + optimizations | ~65 |
| `design-philosophy.md` | 7 design principles guiding architecture | ~55 |

### errors/ — Bug Fixes & Solutions

| File | Topic | Lines |
|------|-------|-------|
| `bug-fixes.md` | Dict attribute error + unawaited coroutine | ~47 |
| `known-gaps.md` | 5 unresolved error/resilience gaps | ~70 |
| `security-gaps.md` | 4 security improvement areas with priority | ~65 |

### guides/ — How-To (step-by-step)

| File | Topic | Lines |
|------|-------|-------|
| `skill-development.md` | Creating Python & Markdown skills | ~76 |
| `cli-reference.md` | CLI commands, flags, examples | ~57 |
| `otel-tracing.md` | OpenTelemetry setup, spans, shutdown | ~100 |

### lookup/ — Quick Reference (tables & schemas)

| File | Topic | Lines |
|------|-------|-------|
| `built-in-skills.md` | All 28+ built-in skills | ~71 |
| `workspace-structure.md` | .workspace/ directory layout | ~68 |
| `configuration.md` | config.json schema + providers | ~120 |
| `implemented-modules.md` | Infrastructure modules already built | ~39 |
| `plan-progress.md` | PLAN.md checkbox tracker (255/299 done, 44 remaining) | ~320 |
| `improvement-roadmap.md` | 10 task categories, 138 subtasks | ~95 |
| `openai-exceptions.md` | OpenAI exception hierarchy + retryability | ~85 |

### examples/ — Working Code

| File | Topic | Lines |
|------|-------|-------|
| `python-skill.md` | Minimal BaseSkill example | ~46 |
| `markdown-skill.md` | Minimal prompt skill example | ~48 |
| `agent-instruction-template.md` | Agent instruction templates with routing YAML | ~68 |

---

## Relationship to Other Context

| Category | Covers |
|----------|--------|
| **project/** (here) | Feature-level: how each component works, APIs, schemas |
| **project-intelligence/** | Domain-level: tech stack, code patterns, naming, standards, security |
| **core/** | Universal standards: MVI, structure, templates |

---

## Harvested From

- `README.md` (330 lines) — extracted 2026-04-04, 2026-04-06
- `FEATURES.md` (588 lines) — extracted 2026-04-16, archived to `.tmp/archive/harvested/2026-04-16/`
- `.tmp/sessions/` (2 session summaries) — extracted 2026-04-30
- `.tmp/tasks/` (10 task categories, 138 subtasks) — extracted 2026-04-30
- `PLAN.md` (158 lines) — extracted 2026-04-30
- `PLAN.md` (61 lines, Round 11) — extracted 2026-05-04
- `src/app.py`, `src/builder.py`, `src/bot/`, `src/core/` — harvested 2026-05-02

## Harvest History

| Date | Operation | Files Created/Updated | Files Archived |
|------|-----------|----------------------|----------------|
| 2026-04-04 | Initial extract | 5 concepts, 2 guides, 3 lookup, 2 examples | — |
| 2026-04-06 | Full harvest | 4 new concepts, 1 errors, 1 lookup + 21 compacted | 1 deprecated file |
| 2026-04-16 | Harvest FEATURES.md + session | 7 concepts, 3 updated (nav, decisions, sessions) | FEATURES.md + media-output session |
| 2026-04-30 | Harvest sessions, tasks, PLAN.md, external ctx | 1 concept, 1 guide, 3 lookup, 1 updated (nav) | — |
| 2026-05-02 | Harvest source code (app, builder, bot/, routing) | 3 new concepts, 2 updated (arch, tech-domain), 1 updated (config), 1 updated (nav) | — |
| 2026-05-02 | Context harvest: Round 4 PLAN.md, config split, module updates | 2 updated lookup (plan-progress, implemented-modules), 1 updated (nav) | — |
| 2026-05-02 | Context harvest: Round 4 remaining items sync | 1 updated lookup (plan-progress: +18 remaining items, totals 50/83) | — |
| 2026-05-02 | Context harvest: deep codebase scan | 5 new concepts (event-bus, dedup, security, noncritical-errors, step-orchestrator), 2 updated concepts (middleware-pipeline v2, architecture-overview), 2 updated lookup (implemented-modules, plan-progress v4), 1 updated (nav) | — |
| 2026-05-02 | add-context --update: created project-intelligence/ | 1 new (technical-domain.md v1.0), 1 new (navigation.md v1.0), 1 updated (project/nav v1.5) | — |
| 2026-05-02 | Harvest: PLAN.md Round 5 + add-context update | 2 updated (plan-progress v5.0, tech-domain v1.1), 2 updated (nav v1.1, project nav v1.6) | — |
| 2026-05-04 | Harvest: Rounds 7-9 + 138 commits since last harvest | 3 updated (tech-domain v2.0, plan-progress v7.0, implemented-modules v2.0), 2 updated (arch-overview v2.2, project nav v1.8) | — |
| 2026-05-04 | Harvest: Round 9 progress (9/20 done) + pattern updates | 3 updated concepts (graceful-shutdown v1.1, dedup v1.1, scheduler v1.1), 3 updated lookup (plan-progress v8.0, modules v2.1, arch-overview v2.3), 1 updated (tech-domain v2.1, nav v1.9) | — |
| 2026-05-04 | Harvest: Round 10 full sync (PLAN.md 308 lines rechecked) | 3 updated (plan-progress v12.0, tech-domain v2.5, nav v2.0) | — |
| 2026-05-04 | Harvest: source code sync (30 commits, event bus +10, lazy args, startup.py) | 3 updated concepts (event-bus v1.2), 2 updated lookup (implemented-modules v3.0, plan-progress v13.0), 1 updated (tech-domain v2.8, nav v2.2) | PLAN.md → .tmp/archive/ |
| 2026-05-04 | Harvest: PLAN.md Round 11 (10/15 done, 5 remaining) | 1 new concept (code-review-decisions), 2 new errors (known-gaps, security-gaps), 2 updated (plan-progress v14.0, nav v2.3) | PLAN.md → .tmp/archive/ |
| 2026-05-05 | Harvest: PLAN.md Round 12 + templates + philosophy | 2 new (design-philosophy, agent-instruction-template), 1 updated (plan-progress v15.0), 1 updated (nav v2.4) | PLAN.md → .tmp/archive/ |
| 2026-05-06 | Harvest: Round 13 PLAN.md + 30 commit codebase sync | 4 updated (plan-progress v16.0, implemented-modules v4.0, arch-overview v2.6, tech-domain v2.0), 2 updated (nav v2.5, project nav v2.5) | PLAN.md → .tmp/archive/ |
| 2026-05-07 | Harvest: Round 14 PLAN.md + incremental persistence + retry budget | 4 updated (plan-progress v18.0, arch-overview v2.8, tech-domain v2.3, known-issues v6.4), 2 updated (nav v2.6, modules count) | PLAN.md → .tmp/archive/ |

## Related

- `../project-intelligence/technical-domain.md` — Stack, architecture decisions
- `../project-intelligence/navigation.md` — Business/tech domain overview
