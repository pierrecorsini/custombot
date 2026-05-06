<!-- Context: project-intelligence/guides/optimization-patterns | Priority: high | Version: 1.3 | Updated: 2026-05-06 -->

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

## Explicit SQLite Transactions for Batch Inserts

When bulk inserts call `INSERT` per row without a transaction, each statement triggers an independent fsync. Wrapping in `BEGIN IMMEDIATE / COMMIT` reduces fsync overhead by 10–100x.

```python
cursor.execute("BEGIN IMMEDIATE")
try:
    for entry in entries:
        cursor.execute("INSERT INTO ...", entry)
    cursor.execute("COMMIT")
except Exception:
    cursor.execute("ROLLBACK")
    raise
```

📂 `src/vector_memory/__init__.py` — `_insert_entries()`

## WAL-Protected Append for Persistence

When buffered appends can lose data on crash. Write to temp, atomically commit as WAL, append to main file, then remove WAL. On startup, replay committed entries.

```python
def _wal_append(self, lines):
    self._wal_tmp_file.write_text(content)
    self._wal_tmp_file.replace(self._wal_file)  # atomic commit
    with self._queue_file.open("a") as f:
        f.write(content); f.flush(); os.fsync(f.fileno())
    self._wal_file.unlink()  # committed
```

📂 `src/message_queue_persistence.py` — `_wal_append()`, `_replay_wal()`

## Msgpack+Base64 Serialization for Queue Lines

When JSON serialization is ~3–5× slower than msgpack for structured data. Encode each line as base64(msgpack(dict)). JSON fallback on read for backward compat.

```python
def _encode_record(data):
    return base64.b64encode(msgpack_dumps(data)).decode("ascii")
```

📂 `src/message_queue_persistence.py` — `_encode_record()`, `_decode_line()`

## DB Error Graceful Degradation

When sqlite-level errors (corruption, extension unavailable) should not crash the app. Catch `sqlite3.Error` separately from API errors — don't queue for retry (retrying won't fix DB corruption). Propagate search errors so callers can fall back to text-based search.

```python
except sqlite3.Error as exc:
    log_noncritical(VECTOR_MEMORY_FALLBACK, f"DB error: {exc}")
    return []  # or -1 for save, letting caller degrade gracefully
```

📂 `src/vector_memory/__init__.py` — `save()`, `save_batch()`, `search()`, `list_recent()`, `count()`

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
