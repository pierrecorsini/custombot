<!-- Context: project/concepts/graceful-shutdown | Priority: medium | Version: 1.1 | Updated: 2026-05-04 -->

# Concept: Graceful Shutdown

**Core Idea**: Ordered cleanup across all components when the bot receives SIGINT/SIGTERM. Stops accepting new messages, waits for in-flight operations to complete (with timeout), then tears down components in reverse dependency order. Uses `ShutdownContext` dataclass and `AppComponents.to_shutdown_context()` factory to centralize teardown state.

**Source**: `src/shutdown.py`, `src/lifecycle.py`, `src/app.py`

---

## Key Points

- **Signal handlers**: Catches SIGINT (Ctrl+C) and SIGTERM for clean exit
- **Stop accepting first**: New messages are rejected immediately on shutdown signal
- **In-flight tracking**: Semaphore-based tracking of active LLM calls with configurable timeout
- **Parallel pre-steps**: `config_watcher.stop()` and `workspace_monitor.stop()` run concurrently via `asyncio.gather()`
- **Factory pattern**: `AppComponents.to_shutdown_context()` builds `ShutdownContext` from populated state (centralized, type-safe)
- **Reverse-order teardown**: Components shut down in reverse dependency order via `ShutdownContext` dataclass
- **Data safety**: All pending state persisted before closing database connections

---

## Shutdown Sequence

```
Signal received (Ctrl+C / SIGTERM)
       │
       ▼
1. Stop accepting new messages
   └── shutdown.request_shutdown()
       │
       ▼
2. Cancel message poller task
       │
       ▼
3. Parallel stop: config_watcher + workspace_monitor
   └── asyncio.gather(watcher.stop(), monitor.stop())
       │
       ▼
4. Build ShutdownContext via AppComponents.to_shutdown_context()
       │
       ▼
5. perform_shutdown(ShutdownContext) — ordered teardown
   └── Stop scheduler, health, channel, DB, etc.
       │
       ▼
   Done ✓
```

---

## Codebase

- `src/shutdown.py` — `GracefulShutdown` manager (signal handling, ordered cleanup)
- `src/lifecycle.py` — `ShutdownContext` dataclass + `perform_shutdown()` ordered teardown
- `src/app.py` — `AppComponents.to_shutdown_context()` factory, parallel pre-steps

## Related

- `concepts/crash-recovery.md` — What happens when shutdown fails (crash)
- `concepts/architecture-overview.md` — Component overview
- `lookup/configuration.md` — Shutdown timeout configuration
