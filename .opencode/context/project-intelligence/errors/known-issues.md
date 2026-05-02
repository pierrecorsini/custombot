<!-- Context: project-intelligence/errors/known-issues | Priority: high | Version: 4.0 | Updated: 2026-05-02 -->

# Known Issues

> Active technical debt, open questions, and current issues. Review weekly.

## Technical Debt (from PLAN.md Round 3 — 15 items remaining)

### Performance (4 items)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| No concurrency semaphore on `_on_message()` | OOM under load, LLM rate limit exhaustion | High | Add `max_concurrent_messages` semaphore (default 10) |
| `executor.shutdown(wait=False)` orphans work | Data loss on crash (pending DB writes, vector batches) | High | Use `wait=True` with timeout |
| No embedding model change detection | Vectors silently incompatible after model swap | Medium | Store model name in metadata table |
| No SQLite connection pooling | 3 independent DB connections, no shared WAL mode | Medium | Create shared `ConnectionPool` abstraction |

### Error Handling (4 items)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Silent error swallowing in `_from_dict()` | Malformed config returns defaults with no warning | Medium | Raise `ConfigurationError` with log |
| Inconsistent `_load_pending()` logging | Corruption equally invisible in repair path | Medium | Unify logging level for both paths |
| TOCTOU race in `Memory.ensure_workspace()` | Concurrent coroutine file creation race | Medium | Atomic `os.O_EXCL` open or lock |
| Shared task dict mutation in scheduler | Iterator invalidation during `_execute_task()` | Medium | Snapshot or copy-on-write pattern |

### Testing (5 items)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Missing `__all__` exports in most modules | Accidental internal imports | Medium | Add to all public modules |
| Duplicate `test_routing.py` | Double-discovery, conflicting results | Medium | Remove root-level, keep `tests/unit/` |
| No config hot-reload integration test | Watcher bugs undetected | High | Test ConfigWatcher with mtime changes |
| No property-based `_from_dict()` roundtrip test | Missing field mappings undetected | High | Add hypothesis roundtrip test |
| No shared `Bot` test fixture | Test duplication, inconsistent isolation | Medium | Add `conftest.py` fixture |

### Security (2 items from PLAN.md Round 4)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| `Config.__repr__()` leaks API key | Secret exposure in traces/debugger | High | Override `__repr__` with redaction |
| No `IncomingMessage` field validation | Injection via crafted IDs | Medium | Add format checks for message_id, chat_id, sender_id |

## Insights & Lessons Learned

### What Works Well
- [Add patterns here]

### What Could Be Better
- [Add areas here]

### Gotchas for Maintainers
- [Add gotchas here]

## Archive (Resolved Items)

Resolved items moved here for historical reference.

### Resolved: [Item]
- **Resolved**: [Date]
- **Resolution**: [What was decided/done]
- **Learnings**: [What we learned]

## Codebase References

- `src/` — Core application modules
- `channels/` — Communication channels
- `.workspace/logs/` — Log files for issue diagnosis

## Related Files

- `lookup/decisions-log.md` — Past decisions informing current state
- `concepts/business-domain.md` — Business context for priorities
- `concepts/architecture.md` — Technical context for current state
- `errors/bug-fixes.md` — Past bugs and fixes applied
