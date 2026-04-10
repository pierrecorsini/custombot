<!-- Context: development/guides/optimization-patterns | Priority: high | Version: 1.0 | Updated: 2026-03-21 -->

# Guide: Performance Optimization Patterns

**Purpose**: Common patterns for optimizing Python async applications

**Source**: Harvested from `.tmp/sessions/2025-03-21-code-optimization/context.md`

---

## Core Concept

Apply targeted optimizations to eliminate O(n²) operations, prevent memory leaks, and ensure non-blocking async I/O. Focus on hot paths: message processing, database lookups, and HTTP clients.

---

## Pattern 1: O(n²) → O(1) with Index

**Problem**: Linear search through message list for deduplication

**Solution**: Maintain a separate index (set/dict) for O(1) lookups

```python
# Before: O(n) - scans entire list
def is_duplicate(msg_id: str, messages: list) -> bool:
    return any(m['id'] == msg_id for m in messages)

# After: O(1) - direct lookup
class MessageStore:
    def __init__(self):
        self._messages = []
        self._id_index: set[str] = set()
    
    def is_duplicate(self, msg_id: str) -> bool:
        return msg_id in self._id_index
```

---

## Pattern 2: Bounded Collections

**Problem**: Unbounded dictionaries cause memory leaks in long-running processes

**Solution**: Use `collections.OrderedDict` with max size, evict oldest

```python
from collections import OrderedDict

class BoundedDict(OrderedDict):
    def __init__(self, max_size: int = 1000):
        super().__init__()
        self._max_size = max_size
    
    def __setitem__(self, key, value):
        if len(self) >= self._max_size:
            self.popitem(last=False)  # Remove oldest
        super().__setitem__(key, value)
```

---

## Pattern 3: HTTP Client Pooling

**Problem**: Creating new HTTP client per request is expensive

**Solution**: Share a single client instance across the application

```python
# src/http_client.py
import httpx

_client: httpx.AsyncClient | None = None

async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30.0)
    return _client

# Usage in multiple modules
client = await get_client()
response = await client.get(url)
```

---

## Pattern 4: LLM Request Timeouts

**Problem**: LLM calls can hang indefinitely, blocking the event loop

**Solution**: Always set timeout on external API calls

```python
import httpx
import asyncio

async def call_llm(prompt: str) -> str:
    async with asyncio.timeout(60.0):  # 60 second timeout
        response = await httpx.post(
            LLM_ENDPOINT,
            json={"prompt": prompt},
            timeout=30.0  # HTTP timeout
        )
    return response.json()['text']
```

---

## Quick Reference

| Issue | Pattern | Complexity |
|-------|---------|------------|
| Linear search | Index/set lookup | O(n) → O(1) |
| Memory growth | Bounded dict | Fixed size |
| HTTP overhead | Shared client | 1 connection |
| Hanging calls | asyncio.timeout | Fails fast |

---

## Codebase Reference

- `src/db.py` - Message deduplication with index
- `src/llm.py` - LLM client with timeout
- `src/bridge_manager.py` - Shared HTTP client
- `channels/whatsapp.py` - HTTP client usage

---

## Related

- `../principles/clean-code.md` - Code quality standards
- `../../../core/standards/security-patterns.md` - Security patterns
