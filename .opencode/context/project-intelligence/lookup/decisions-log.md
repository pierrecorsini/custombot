<!-- Context: project-intelligence/lookup/decisions-log | Priority: high | Version: 4.0 | Updated: 2026-05-02 -->

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

## Decision: Media Output via Callback Injection

**Date**: 2026-04-12 | **Status**: Decided

**Context**: Needed to add audio (TTS) and PDF report output to the bot without changing existing skill return types or breaking the tool executor interface.
**Decision**: Use callback injection (Option 2c) — thread a `send_media` callback from the channel through the bot and ToolExecutor to the skill.
**Rationale**: Keeps existing skills untouched. Skill generates file, calls callback, channel handles delivery. Clean separation between generation and transport.
**Alternatives**: Return media path from skill (rejected: changes return type contract), Direct channel access in skills (rejected: breaks layering).

**Impact**:
- **Positive**: No breaking changes, clean layering, extensible to more media types
- **Negative**: Callback threading adds indirection through 3 layers
- **Libraries**: edge-tts (free TTS), xhtml2pdf (pure Python PDF), markdown (HTML conversion)

---

## Decision: Config Module Split

**Date**: 2026-05-02 | **Status**: Decided

**Context**: `src/config/config.py` grew to 785 lines, mixing data model, I/O, logging, and validation concerns.
**Decision**: Split into 3 modules: `config_schema_defs.py` (pure data), `config_loader.py` (I/O + env overrides), `config_validation.py` (validation helpers). Original `config.py` becomes a re-export facade.
**Rationale**: Each module has a single responsibility. Data model changes don't touch I/O code and vice versa.
**Alternatives**: Keep monolithic file (rejected: hard to navigate), Further split into per-section files (rejected: over-engineering for current scale).

---

## Decision: ShutdownContext Dataclass

**Date**: 2026-05-02 | **Status**: Decided

**Context**: `perform_shutdown()` had 12 positional parameters, a maintenance burden when adding new components.
**Decision**: Replace with a `ShutdownContext` frozen dataclass containing all parameters.
**Rationale**: Named parameters prevent ordering bugs, adding new components only requires updating the dataclass.
**Alternatives**: Builder pattern (rejected: over-engineering), dict parameter (rejected: no type safety).

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
