<!-- Context: development/concepts | Priority: high | Version: 1.1 | Updated: 2026-04-16 -->

# Concept: Memory Safety Patterns

**Purpose**: Prevent unbounded memory growth in long-running processes

---

## Core Idea

Place explicit limits on all caches, lists, and buffers to prevent memory leaks in daemons and bots that run indefinitely.

---

## Key Points

- Set max size constants (e.g., `MAX_ITEMS = 100`)
- Implement automatic pruning when limit exceeded
- Use bounded collections (`deque` with `maxlen`)
- Use `LRUDict` for generic bounded key-value caches
- Monitor memory in long-running processes
- Document memory limits in code comments

---

## Quick Example

```python
MAX_TIMESTAMPS = 100

def add_timestamp(timestamps: list, new_ts: float) -> list:
    timestamps.append(new_ts)
    # Automatic pruning
    if len(timestamps) > MAX_TIMESTAMPS:
        timestamps = timestamps[-MAX_TIMESTAMPS:]
    return timestamps

# Or use deque for automatic bounds
from collections import deque
timestamps = deque(maxlen=100)  # Auto-prunes oldest

# Or use LRUDict for generic bounded key-value caches
from src.utils import LRUDict
incoming_lengths = LRUDict(max_size=500)
incoming_lengths["chat_123"] = 42
value = incoming_lengths.pop("chat_123", default=0)
```

---

## Lock Model Conventions

| Lock Type | When to Use | Used In |
|-----------|-------------|---------|
| `threading.Lock` | Mixed sync/async code, blocking I/O (sqlite3), cross-thread access | `vector_memory.py`, `rate_limiter.py`, `llm.py` |
| `asyncio.Lock` | Pure async code, file I/O via `asyncio.to_thread()`, coroutine-safe access | `db.py`, `message_queue.py`, `bot.py` |

**Rule**: If the code calls blocking operations from threads or is invoked from both sync and async contexts → `threading.Lock`. If all access is within async coroutines → `asyncio.Lock`.

---

## When to Apply

- Rate limiters with timestamp tracking
- Message history caches
- In-memory request queues
- Any unbounded list that grows over time
- Per-chat data stores (use `LRUDict`)

---

## Related

- examples/rate-limiter-bounded.md
- concepts/performance-patterns.md
- `src/utils/__init__.py` — `LRUDict` implementation

**Source**: Harvested from session 2026-03-26-code-optimization
