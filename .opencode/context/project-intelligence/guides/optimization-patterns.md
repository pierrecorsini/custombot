<!-- Context: project-intelligence/guides/optimization-patterns | Priority: high | Version: 1.1 | Updated: 2026-05-03 -->

# Optimization Patterns

> Reusable micro-optimization patterns applied to hot paths. Each: concern-isolated, testable, single-commit.

---

## Bounded Concurrency Semaphore

When unlimited async ops exhaust resources. Cap with `asyncio.Semaphore`.

```python
self._sem = asyncio.Semaphore(max_concurrent_messages)  # default 10
async def _on_message(self, msg):
    async with self._sem: await self._process(msg)
```

📂 `src/app.py` — `Application._on_message()`

## Sentinel-Based Lazy Caching

When repeatedly reading env vars is wasteful. Sentinel distinguishes "not yet read" from "cached None".

```python
_SENTINEL = object()
_cached: str | None | type = _SENTINEL
def get_value():
    global _cached
    if _cached is _SENTINEL:
        _cached = os.environ.get("KEY", "").strip() or None
    return _cached
```

📂 `src/security/signing.py` — `get_scheduler_secret()`

## No-Rules Short-Circuit

When routing/lookup has an empty-set common case. Early return before computation.

```python
if not self._rules_list and not self._dirty:
    return (None, None)  # skip stale check + cache + context build
```

📂 `src/routing.py` — `RoutingEngine.match_with_rule()`

## Pre-Compute Once Per Tick

When multiple items re-compute `now` per loop tick. Compute once, pass to consumers.

```python
now = _now()  # once per tick
for task in tasks:
    if self._is_due(task, now): ...
```

📂 `src/scheduler.py` — `_run_loop()` + `_is_due()`

## Direct Return for Read-Only Snapshots

When property creates new dataclass copy on every access. Return directly — safe when event-loop is sole mutator.

```python
@property
def stats(self) -> DedupStats:
    return self._stats  # documented: event-loop thread is sole mutator
```

📂 `src/core/dedup.py` — `DeduplicationService.stats`

## Double Flush Elimination

When method A calls B explicitly, but C already calls B internally. Remove redundant explicit call.

📂 `src/message_queue.py` — `MessageQueue.close()` removed explicit `_flush_write_buffer()`

## Hash Chain for Tamper-Evident Logs

Each audit entry includes SHA-256 of previous line. Opt-in via `chain_hashes=True`.

```python
entry["_prev_hash"] = self._prev_hash
line = json.dumps(entry, default=str)
self._prev_hash = hashlib.sha256(line.encode()).hexdigest()
```

📂 `src/security/audit.py` — `SkillAuditLogger`

## Async Wrapper for Blocking I/O

When sync file I/O blocks the event loop. Wrap with `asyncio.to_thread()`.

```python
corruption = await asyncio.to_thread(self.detect_memory_corruption, chat_id)
```

📂 `src/memory.py` — `read_memory_with_validation()`

## Graceful Degradation (Zero-Rule Retention)

When a reload transiently produces zero rules (e.g. editor truncating file). Retain previous rules, keep stale mtimes to retry next tick.

```python
if len(rules) == 0 and len(previous_rules) > 0:
    log.warning("Reload produced zero routing rules — retaining previous (%d)", len(previous_rules))
    return  # _file_mtimes left stale → next stale-check retries
```

📂 `src/routing.py` — `RoutingEngine.load_rules()`

## Session 2 Optimizations (2026-05-02)

| Optimization | Module | Technique |
|-------------|--------|-----------|
| xxHash for dedup keys | `src/core/dedup.py` | Fast non-crypto hash |
| Pre-compute routing candidates | `src/routing.py` | Lazy pre-computation |
| Scheduler epoch caching | `src/scheduler.py` | Epoch memoization |
| Single-pass response filter | `src/bot/` | One-pass iteration |
| RFC 1918 private IP detection | `src/llm.py` | Network-aware validation |

---

## 📂 Codebase References

All patterns link to specific files above. Full change specs in `.tmp/tasks/code-optimization/subtask_*.json`.

## Related

- `lookup/completed-sessions.md` — Sessions where patterns were applied
- `lookup/decisions-log.md` — Architectural decisions driving optimizations
- `errors/bug-fixes.md` — Bug fixes overlapping with optimization work
