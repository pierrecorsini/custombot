<!-- Context: development/examples | Priority: high | Version: 1.0 | Updated: 2026-03-27 -->

# Example: Bounded Rate Limiter

**Purpose**: Rate limiter with automatic memory safety

---

## Problem

Unbounded timestamp lists grow forever in long-running bots, causing memory leaks over days/weeks of operation.

---

## Solution

```python
from collections import deque
from time import time

class RateLimiter:
    """Rate limiter with bounded memory usage."""
    
    MAX_TIMESTAMPS_PER_CHAT = 100  # Safety limit
    WINDOW_SECONDS = 60
    MAX_REQUESTS = 10
    
    def __init__(self):
        # deque auto-prunes when maxlen exceeded
        self._timestamps: dict[str, deque] = {}
    
    def is_allowed(self, chat_id: str) -> bool:
        """Check if request is allowed, auto-pruning old entries."""
        now = time()
        cutoff = now - self.WINDOW_SECONDS
        
        # Get or create bounded deque
        if chat_id not in self._timestamps:
            self._timestamps[chat_id] = deque(maxlen=self.MAX_TIMESTAMPS_PER_CHAT)
        
        timestamps = self._timestamps[chat_id]
        
        # Remove old entries (within deque's bounds)
        while timestamps and timestamps[0] < cutoff:
            timestamps.popleft()
        
        # Check rate limit
        if len(timestamps) >= self.MAX_REQUESTS:
            return False
        
        timestamps.append(now)
        return True


# Alternative: Manual pruning for list-based implementation
class ListRateLimiter:
    MAX_TIMESTAMPS = 100
    
    def _prune_old(self, timestamps: list, cutoff: float) -> list:
        # Filter old + enforce max size
        filtered = [t for t in timestamps if t > cutoff]
        if len(filtered) > self.MAX_TIMESTAMPS:
            filtered = filtered[-self.MAX_TIMESTAMPS:]
        return filtered
```

---

## Key Design Choices

1. **`deque(maxlen=N)`** - Automatic pruning, O(1) operations
2. **Explicit constant** - `MAX_TIMESTAMPS_PER_CHAT = 100` documents the limit
3. **Prune on access** - Clean up happens during normal operation

---

## When to Use

- Chat bot rate limiting
- API request throttling
- Any timestamp tracking in long-running processes

---

**Source**: `src/rate_limiter.py`  
**Reference**: Harvested from session 2026-03-26-code-optimization
