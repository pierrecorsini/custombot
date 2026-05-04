<!-- Context: project/concepts/monitoring-metrics | Priority: medium | Version: 1.0 | Updated: 2026-04-16 -->

# Concept: Monitoring & Metrics

**Core Idea**: Real-time performance tracking across all bot operations. Tracks LLM latency, end-to-end message latency, queue depth, token usage (prompt + completion), and memory consumption — all queryable via the health endpoint.

**Source**: `FEATURES.md` — Monitoring & Metrics section (archived 2026-04-16)

---

## Key Points

- **LLM latency**: Tracks every API call duration for response time analysis
- **Message latency**: End-to-end time from message receipt to response delivery
- **Queue depth**: Count of pending messages in crash recovery queue
- **Token usage**: Per-session prompt + completion token counts with request count
- **Memory monitor**: Warning at 75% usage, critical at 90% — periodic checks with cache tracking

---

## Metrics Dashboard

```
┌────────────────┐  ┌────────────────┐  ┌────────────┐
│  LLM Latency   │  │  Message       │  │  Queue     │
│                │  │  Latency       │  │  Depth     │
│ track every    │  │ track every    │  │ pending    │
│ LLM API call   │  │ handle_msg     │  │ messages   │
└────────────────┘  └────────────────┘  └────────────┘

┌────────────────┐  ┌────────────────┐
│  Memory Monitor│  │  Token Usage   │
│                │  │                │
│ warning: 75%   │  │ per-session    │
│ critical: 90%  │  │ prompt +       │
│ cache tracking │  │ completion     │
└────────────────┘  └────────────────┘
```

---

## Token Usage Tracking

```json
{
  "prompt_tokens": 15234,
  "completion_tokens": 3892,
  "total_tokens": 19126,
  "request_count": 47
}
```

- Tracked per-session via `threading.Lock` for thread safety
- Accessible via `/health` endpoint

---

## Memory Thresholds

| Level | Usage | Action |
|-------|-------|--------|
| Normal | <75% | No action |
| Warning | 75–90% | Logged warning |
| Critical | >90% | Logged critical + potential degradation |

---

## Codebase

- `src/monitoring/performance.py` — PerformanceMetrics collection
- `src/health/server.py` — HTTP `/health` endpoint exposing metrics
- `src/health/models.py` — Health report data models

## Related

- `concepts/architecture-overview.md` — Component map including monitoring
- `lookup/implemented-modules.md` — Monitoring module reference
