<!-- Context: project/lookup/improvement-roadmap | Priority: medium | Version: 1.0 | Updated: 2026-04-30 -->

# Lookup: Improvement Roadmap

**Purpose**: 10 task categories from `.tmp/tasks/` with objectives, progress, and key files
**Source**: `.tmp/tasks/*/task.json` (10 categories, 138 subtasks total)

---

## Status Overview

| Category | Subtasks | Done | Status | Objective |
|----------|----------|------|--------|-----------|
| architecture-quality | 16 | 0 | planned | Refactor DI, eliminate globals, abstract DB, consolidate config |
| developer-experience | 12 | 0 | planned | Modern project config, docs, tooling |
| features-ux | 16 | 0 | planned | Summarization, multi-channel, enhanced scheduler, plugins |
| llm-agent-improvements | 17 | 0 | planned | Context management, streaming, ReAct loop, multi-model |
| observability-monitoring | 10 | 0 | planned | Prometheus metrics, distributed tracing, cost tracking |
| performance-scalability | 12 | 0 | planned | Async I/O, caching, memory optimization for 1000+ chats |
| phase9-remaining | 19 | 0 | planned | PLAN.md phase 9 tasks (perf, errors, security, observability, tests) |
| reliability-resilience | 14 | 0 | planned | Circuit breakers, DB resilience, WhatsApp reconnect, error recovery |
| security-hardening | 7 | 3 | **active** | Prompt injection, shell sandbox, path hardening, rate limits |
| testing-qa | 15 | 7 | **active** | Test coverage, mocks, property-based tests, linting, typing |

---

## Key Reference Files by Category

### architecture-quality
`src/builder.py`, `src/llm.py`, `src/db/db.py`, `src/config/config.py`, `src/routing.py`

### developer-experience
`main.py`, `pyproject.toml`, `README.md`, `src/config/config.py`

### features-ux
`src/core/topic_cache.py`, `src/channels/base.py`, `src/channels/whatsapp.py`, `src/scheduler.py`

### llm-agent-improvements
`src/llm.py`, `src/bot.py`, `src/vector_memory/`, `src/core/context_builder.py`, `src/routing.py`

### observability-monitoring
`src/monitoring/performance.py`, `src/health/server.py`, `src/logging/logging_config.py`

### performance-scalability
`src/memory.py`, `src/vector_memory/`, `src/db/db.py`, `src/core/instruction_loader.py`

### phase9-remaining
`src/bot.py`, `src/llm.py`, `src/skills/__init__.py`, `src/core/tool_executor.py`, `src/security/prompt_injection.py`

### reliability-resilience
`src/llm.py`, `src/db/`, `src/vector_memory/`, `src/channels/whatsapp.py`, `src/memory.py`

### security-hardening (3/7 done)
`src/security/prompt_injection.py` ✅, `src/core/context_builder.py`, `src/skills/builtin/shell.py`

### testing-qa (7/15 done)
`tests/`, `src/bot.py`, `src/llm.py`, `src/routing.py`, `src/scheduler.py`

---

## Exit Criteria Highlights

### Must-have before production (architecture + reliability + security)
- No module-level mutable globals in hot paths
- Circuit breakers on all LLM calls with half-open state
- WAL mode + connection pooling for all SQLite databases
- Prompt injection detection on all user input paths
- Test coverage ≥80%

### Nice-to-have (features + observability + performance)
- Multi-channel support (Telegram, Discord)
- Conversation summarization when history exceeds token limits
- Prometheus `/metrics` endpoint with standard exposition format
- Redis caching backend option
- Per-component memory monitoring

---

## Subtask Format

Each subtask in `.tmp/tasks/{category}/subtask_XX.json` contains:
```json
{
  "id": "category-NN",
  "title": "Short task description",
  "status": "pending|completed",
  "depends_on": ["category-MM"],
  "parallel": true,
  "acceptance_criteria": ["..."],
  "deliverables": ["file.py"],
  "completion_summary": null
}
```

---

## Codebase

- `.tmp/tasks/` — 138 subtask JSON files across 10 categories
- `PLAN.md` — Completed improvement items (45/60 done)

## Related

- `lookup/plan-progress.md` — PLAN.md checkbox tracker (15 remaining)
- `lookup/implemented-modules.md` — Modules already built
- `project-intelligence/technical-domain.md` — Tech stack and patterns
