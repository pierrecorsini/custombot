<!-- Context: project-intelligence/lookup/decisions-log | Priority: high | Version: 3.0 | Updated: 2026-04-06 -->

# Decisions Log

> Record major architectural and business decisions with full context. Prevents "why was this done?" debates.

## Decision Template

```markdown
## [Title]
Date: YYYY-MM-DD | Status: [Decided/Pending/Under Review/Deprecated]

Context: [What prompted this decision?]
Decision: [What was decided?]
Rationale: [Why this choice?]
Alternatives: [What was rejected and why?]
Impact: [What this enables, trades off, or risks]
Related: [Links to PRs, issues, docs]
```

---

## Decision: Native Python via neonize

**Date**: 2026-03 | **Status**: Decided

**Context**: Needed WhatsApp integration without Node.js dependency or subprocess management overhead.
**Decision**: Use neonize (ctypes bindings to whatsmeow/Go) for direct Python-to-WhatsApp communication.
**Rationale**: Eliminates Node.js dependency, removes HTTP bridge latency, uses single SQLite session file.
**Alternatives**: whatsapp-web.js + bridge subprocess (rejected: adds complexity), Baileys HTTP bridge (rejected: latency).

**Impact**:
- **Positive**: Pure Python stack, lower latency, simpler deployment
- **Negative**: Dependent on neonize library maintenance
- **Risk**: neonize API changes require code updates

---

## Decision: SQLite for Storage

**Date**: 2026-03 | **Status**: Decided

**Context**: Need conversation and session persistence for single-instance bot.
**Decision**: SQLite via aiosqlite for all storage needs.
**Rationale**: Embedded, zero-config, perfect for single-instance deployment.
**Alternatives**: PostgreSQL (rejected: overkill), Redis (rejected: no persistence needed).

---

## Decision: Per-Chat Workspaces

**Date**: 2026-03 | **Status**: Decided

**Context**: Conversations need isolation to prevent cross-contamination.
**Decision**: Each chat gets its own directory under `.workspace/<chat_id>/`.
**Rationale**: Clean separation, easier debugging, simple file-based isolation.

---

## Decision: .workspace/ for All Runtime Files

**Date**: 2026-03 | **Status**: Decided

**Context**: Runtime files (logs, DB, session, per-chat data) scattered across project.
**Decision**: Centralize all dynamic content in `.workspace/`.
**Rationale**: Clear separation of code vs data, simpler backup and cleanup.

---

## Deprecated Decisions

| Decision | Date | Replaced By | Why |
|----------|------|-------------|-----|

## Codebase References

- `src/bot.py` — Orchestrator implementing these decisions
- `channels/whatsapp.py` — neonize integration
- `.workspace/` — Runtime file centralization

## Related Files

- `concepts/architecture.md` — How decisions shape the architecture
- `concepts/business-tech-bridge.md` — Business-technical trade-offs
- `errors/known-issues.md` — Open questions that may become decisions
