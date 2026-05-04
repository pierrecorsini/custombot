<!-- Context: project/concepts/stealth-patterns | Priority: medium | Version: 1.0 | Updated: 2026-04-06 -->

# Concept: Stealth / Anti-Detection Patterns

**Core Idea**: Human-like timing delays using log-normal distributions for natural variation. Simulates reading, thinking, and typing behavior before sending replies, with per-chat cooldowns to avoid bot-detection patterns.

**Source**: `README.md` — Stealth / Anti-Detection section

---

## Key Points

- **Log-normal distribution**: All delays use log-normal random values for natural, non-uniform timing
- **Read delay**: Scales by message length — short messages 0.3–2.0s, long messages 1.5–5.0s
- **Type delay**: Response length / 50-80 chars/sec, capped at 8s — simulates human typing speed
- **Typing pause**: 30% chance of 0.5–2.0s mid-typing pause (simulates re-reading)
- **Per-chat cooldown**: 3s minimum between replies to same chat

---

## Timing Pipeline

```
Incoming message
       │
       ▼
1. Read delay ─── log-normal, scaled by msg length
   <50 chars:  0.3–2.0s
   <200 chars: 0.8–3.5s
   200+ chars: 1.5–5.0s
       │
       ▼
2. Think delay ── log-normal 0.5–4.0s
       │
       ▼
3. Send "typing..." indicator
       │
       ▼
4. Type delay ─── response_len / (50-80 chars/s), capped 8s
       │
       ▼
5. Typing pause ── 30% chance, 0.5–2.0s (mid-typing re-read)
       │
       ▼
6. Per-chat cooldown: 3s minimum between replies
       │
       ▼
   Send reply to WhatsApp
```

---

## Quick Example

```python
# Log-normal delay pattern (simplified)
import random, math

def log_normal_delay(mean, sigma=0.5):
    return max(0.1, random.lognormvariate(math.log(mean), sigma))

read_delay = log_normal_delay(1.5)  # ~0.3-5.0s
think_delay = log_normal_delay(2.0) # ~0.5-4.0s
type_delay = min(8.0, len(response) / random.uniform(50, 80))
```

---

## Codebase

- `src/channels/stealth.py` — Stealth timing helpers and delay calculations
- `src/channels/whatsapp.py` — Integrates stealth delays into message sending flow

## Related

- `concepts/architecture-overview.md` — Overall system design
- `concepts/react-loop.md` — Where stealth fits in the message pipeline
