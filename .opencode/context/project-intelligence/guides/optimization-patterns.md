<!-- Context: project-intelligence/guides/optimization-patterns | Priority: high | Version: 1.6 | Updated: 2026-05-07 -->

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

## Msgpack+Base64+CRC32 Serialization for Queue Lines

When JSON serialization is ~3–5× slower than msgpack for structured data. Encode each line as `CRC32:base64(msgpack(dict))`. CRC32 guards against truncated writes and bit-rot. JSON fallback on read for backward compat. Legacy unguarded msgpack also supported.

```python
def _encode_record(data):
    payload = msgpack_dumps(data)
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    return f"{crc:08x}:{base64.b64encode(payload).decode('ascii')}"
```

📂 `src/message_queue_persistence.py` — `_encode_record()`, `_decode_line()`, `_verify_crc32()`

## DB Error Graceful Degradation

When sqlite-level errors (corruption, extension unavailable) should not crash the app. Catch `sqlite3.Error` separately from API errors — don't queue for retry (retrying won't fix DB corruption). Propagate search errors so callers can fall back to text-based search.

```python
except sqlite3.Error as exc:
    log_noncritical(VECTOR_MEMORY_FALLBACK, f"DB error: {exc}")
    return []  # or -1 for save, letting caller degrade gracefully
```

📂 `src/vector_memory/__init__.py` — `save()`, `save_batch()`, `search()`, `list_recent()`, `count()`

## NullObject for Optional Dependencies
Replace `Optional[X]` + None-checks with NullObject satisfying Protocol with safe no-ops.
📂 `src/monitoring/memory.py` — `NullMemoryMonitor`

## Registry Pattern for Discoverable Health Checks
Centralize scattered health checks into a registry with standardized `HealthCheckResult` signatures.
📂 `src/health/registry.py` — `HealthCheckRegistry`

## Shared Connection Pool for Embedding HTTP
Replace per-request `httpx.AsyncClient` with shared, long-lived client with connection pooling.
📂 `src/builder.py` — shared `httpx.AsyncClient`

## TTL-Based Eviction for LRU Caches
Configurable TTL on `BoundedOrderedDict` reclaims idle locks from transient group chats.
📂 `src/utils/` — `BoundedOrderedDict._ttl`, `DEFAULT_LOCK_CACHE_TTL`

## Per-Resource Circuit Breaker
Per-name circuit breakers isolate failures per skill or external resource.
📂 `src/core/skill_breaker_registry.py` — `SkillBreakerRegistry` with LRU eviction

## Bounded Semaphore for Event Fan-Out
Cap concurrent handler invocations per EventBus emission to prevent unbounded coroutine creation. Lazy-initialized on first `emit()` to avoid event-loop timing issues.
📂 `src/core/event_bus.py` — `_emit_semaphore`

## Coalesced Debounce Flush for Database Writes
When multiple rapid `upsert_chat` calls trigger within the debounce window, coalesce into a single scheduled flush via `loop.call_later`. Reduces redundant disk I/O on burst activity.

```python
if self._chats_flush_handle is None:
    loop = asyncio.get_running_loop()
    self._chats_flush_handle = loop.call_later(
        self._chats_save_interval,
        lambda: asyncio.ensure_future(self._scheduled_chats_flush()),
    )
```

📂 `src/db/db.py` — `_scheduled_chats_flush()`, `_chats_flush_handle`

## Per-Chat Latency Percentile Tracking
Bounded LRU deques (top-N chats by volume) store per-message latency samples. Compute `p50`/`p95`/`p99` per chat to identify slow conversations without global averaging.

📂 `src/monitoring/performance.py` — `_chat_latencies`, `get_top_chat_latencies()`

## Error-Rate Alerting with Cooldown
Configurable per-window alert thresholds check error rates over sliding windows. Structured `error_rate_exceeded` warning enables external alerting (ELK, Datadog). Cooldown prevents spam.

📂 `src/monitoring/performance.py` — `_check_error_rate_thresholds()`, `DEFAULT_ERROR_ALERT_THRESHOLDS`

## Session 2 Optimizations (2026-05-02)

xxHash dedup keys (`core/dedup.py`), pre-compute routing candidates (`routing.py`), scheduler epoch caching (`scheduler.py`), single-pass response filter (`bot/`), RFC 1918 private IP detection (`llm.py`).

---

📂 All patterns link to specific source files above.

## Related

- `lookup/completed-sessions.md` — Sessions where patterns were applied
- `lookup/decisions-log.md` — Architectural decisions driving optimizations
