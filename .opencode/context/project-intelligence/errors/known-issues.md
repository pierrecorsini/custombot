<!-- Context: project-intelligence/errors/known-issues | Priority: high | Version: 5.0 | Updated: 2026-05-04 -->

# Known Issues

> Active technical debt, open questions, and current issues. Review weekly.

## Technical Debt (from PLAN.md Round 10 — 23 items remaining)

### Architecture & Refactoring (3 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Move `llm_error_classifier.py` into `src/llm/` package | Top-level clutter (40 entries in `src/`) | Medium | Re-export `LLMClient` from `__init__.py` for compat |
| Add `__slots__` to `QueuedMessage` dataclass | ~40% per-instance memory waste | Medium | Use `slots=True` like other high-frequency dataclasses |
| Extract `MiddlewareChain` from `MessagePipeline` | Hard-to-debug closure stack in tracebacks | Low | Named class with `__repr__` showing middleware names |

### Performance & Scalability (4 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Pre-warm `FileHandlePool` at startup | N serialized `open()` calls during crash recovery | Medium | Call `get_or_open()` for known chats after `db.connect()` |
| Re-serialize tool call arguments in `execute_tool_call()` | Memory duplication for large payloads (base64) | Medium | Store raw JSON in `ToolLogEntry`, parse lazily on render |
| No batch for `record_outbound()` writes | N individual dict ops during burst delivery | Medium | Buffer outbound recordings, flush in single batch |
| Use `msgpack` for `MessageQueue` persistence | JSON ~3-5× slower than msgpack for 10-field objects | Low | Switch to msgpack-binary JSONL, keep JSON fallback for recovery |

### Error Handling & Resilience (4 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| `embed_http` leaks on non-degradation exception in `_step_vector_memory()` | Connection leak if `vm.connect()` raises | Medium | Move `aclose()` to `finally` block |
| No structured event for generation conflicts | Write-conflict frequency untracked in monitoring | Medium | Emit `generation_conflict` event with generation numbers |
| `OSError` (disk full) in `_deliver_response()` not caught | Lost response text when persistence fails | Medium | Catch `OSError`, return text to user anyway |
| No startup health event via EventBus | External subscribers can't detect successful startup | Low | Emit `startup_complete` with component count and timing |

### Testing & Quality (5 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| No test for `Bot._send_to_chat()` | Shared send+dedup+event helper untested | Medium | Test with/without channel, verify dedup tracking |
| No test for `_swap_config()` atomicity | Atomic replacement guarantee unverified | Medium | Inspect `_config` before/after concurrent swap |
| No test for `_step_vector_memory()` dedicated URL degradation | Complex path with dedicated `embed_http` untested | Medium | Verify dedicated client properly closed on probe failure |
| No property-based test for `outbound_key()` hash | Hash determinism/collision resistance unverified | Medium | Use hypothesis with `st.text()` strategies |
| No integration test for full `_on_message` timeout path | Most critical production path only has unit coverage | High | Verify semaphore release, timeout logging, subsequent processing |

### Security (3 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| `sender_name` not sanitized in validation layer | ANSI escapes/control chars corrupt logs/JSON | Medium | Truncate to 200 chars, strip non-printable in `__post_init__` |
| No `Content-Length` header validation on HealthServer | Large header causes memory allocation before path rejection | Low | Reject `Content-Length` > 0 on GET endpoints |
| No `ToolLogEntry.name` length validation | Tool name >4096 chars causes OSError in audit log | Low | Truncate to 200 chars in constructor |

### DevOps & CI (4 remaining)

| Item | Impact | Priority | Mitigation |
|------|--------|----------|------------|
| Missing `Ruff` `PERF` ruleset | Performance anti-patterns in hot paths undetected | Low | Add `PERF` as non-blocking initially |
| No `pip-audit` SARIF upload to GitHub Security | Vulnerabilities only in job log, not Security tab | Low | Use `--format sarif` + `upload-sarif` action |
| `ruff` version mismatch (CI vs pyproject.toml) | Local/CI linting may differ | Medium | Pin `ruff==0.15.12` in pyproject.toml |
| No `pytest-timeout` in CI | Hung tests stall CI indefinitely | Medium | Add `--timeout=120` to CI pytest invocation |
| No `PLAN.md` checkbox syntax validation | Malformed checkboxes invisible to tracking scripts | Low | Add `scripts/check_plan_syntax.py` to CI |

## Insights & Lessons Learned

### What Works Well
- Message starvation detection catches silent WhatsApp disconnections
- Normalizing at channel boundary prevents unit mismatch propagation

### What Could Be Better
- Format conversion helpers should be integration-tested end-to-end
- External library field coverage should be verified against real client behavior

### Gotchas for Maintainers
- WhatsApp timestamps are milliseconds, not seconds — always normalize at the boundary
- neonize library may not populate all WhatsApp protocol fields — verify PTT fields separately

## Archive (Resolved Items)

### Resolved: Round 3 Technical Debt (15 items — ALL completed 2026-05-04)
- **Resolved**: 2026-05-04
- **Resolution**: All 15 remaining items from PLAN.md Round 3 completed across Rounds 4-9
- **Items**: Concurrency semaphore, executor shutdown, embedding detection, connection pooling, _from_dict error raising, TOCTOU-safe seeding, scheduler mutation guard, __all__ exports, duplicate test removal, config hot-reload test, property-based config test, shared Bot fixture, Config.__repr__ redaction, IncomingMessage validation, Dockerfile pinning

### Resolved: Round 9 Technical Debt (11 items — ALL completed 2026-05-04)
- **Resolved**: 2026-05-04
- **Resolution**: All 11 remaining items from PLAN.md Round 9 completed
- **Items**: Atomic file writes in TaskScheduler, stdin read timeout, _classify_main_loop_error test, timeout path queue state test, hot-reload denylist test, Application._transition() rollback test, retry sleep cap in RoutingEngine, task validation in TaskScheduler._load(), config.example.json CI sync, Docker BuildKit caching, coverage regression gate

## Codebase References

- `src/` — Core application modules
- `channels/` — Communication channels
- `src/channels/whatsapp.py` — Timestamp normalization boundary
- `src/channels/neonize_backend.py` — Voice note PTT fields
- `.workspace/logs/` — Log files for issue diagnosis

## Harvested From

- Session snapshots (3 files) — 2026-05-04

## Related Files

- `errors/bug-fixes.md` — Past bugs and fixes (Fixes 8-10)
- `project/lookup/plan-progress.md` — Full PLAN.md progress tracker (Round 10 remaining)
- `concepts/architecture.md` — Technical context for current state
