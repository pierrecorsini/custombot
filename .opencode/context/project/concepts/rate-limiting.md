<!-- Context: project/concepts/rate-limiting | Priority: medium | Version: 1.0 | Updated: 2026-04-16 -->

# Concept: Rate Limiting

**Core Idea**: Sliding window rate limiter with two tiers — per-chat (30 msgs/60s) and per-skill (10 calls/60s for expensive skills). Uses timestamp-based sliding window algorithm with LRU eviction to bound memory usage.

**Source**: `FEATURES.md` — Rate Limiting section (archived 2026-04-16)

---

## Key Points

- **Two-tier limits**: Per-chat (general message rate) + per-skill (expensive operations)
- **Sliding window**: Tracks timestamps, counts only those within `now - 60s` window
- **Expensive skills**: `web_search`, `web_research`, `shell`, `memory_save` — 10 calls/60s
- **Memory bounded**: Max 1000 tracked chats (LRU eviction), max 100 timestamps per chat
- **Thread-safe**: Uses `threading.Lock` for concurrent access

---

## Algorithm

```
Sliding Window:
  timestamps: [t1, t2, t3, ..., tN]
  window_start = now - 60s
  count = timestamps after window_start

  count < limit?  → ALLOW
  count >= limit? → REJECT + warn

Memory protection:
  • Max 1000 tracked chats (LRU eviction)
  • Max 100 timestamps per chat
```

---

## Rate Limits

| Tier | Limit | Window | Scope |
|------|-------|--------|-------|
| Per-chat | 30 messages | 60s | Each chat independently |
| Per-skill | 10 calls | 60s | Each expensive skill per chat |

---

## Expensive Skills

| Skill | Why Rate-Limited |
|-------|-----------------|
| `web_search` | External API calls, latency |
| `web_research` | Search + crawl, high latency |
| `shell` | System resource usage |
| `memory_save` | Embedding API call, vector DB write |

---

## Quick Example

```python
# Simplified sliding window check
def is_allowed(timestamps, limit=30, window=60):
    now = time.time()
    recent = [t for t in timestamps if t > now - window]
    return len(recent) < limit
```

---

## Codebase

- `src/rate_limiter.py` — Sliding window rate limiter (per-chat + per-skill, LRU eviction)

## Related

- `concepts/react-loop.md` — Where rate limiting sits in the pipeline
- `lookup/implemented-modules.md` — Rate limiter module reference
