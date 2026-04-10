<!-- Context: development/concepts | Priority: high | Version: 1.0 | Updated: 2026-03-27 -->

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
```

---

## When to Apply

- Rate limiters with timestamp tracking
- Message history caches
- In-memory request queues
- Any unbounded list that grows over time

---

## Related

- examples/rate-limiter-bounded.md
- concepts/performance-patterns.md

**Source**: Harvested from session 2026-03-26-code-optimization
