<!-- Context: project/concepts/routing-engine | Priority: high | Version: 1.0 | Updated: 2026-04-04 -->

# Concept: Message Routing Engine

**Core Idea**: Every incoming message is matched against priority-sorted routing rules based on sender, recipient, channel, content regex, and direction flags (`fromMe`/`toMe`). The first matching rule determines which instruction file (persona) the LLM receives.

**Source**: `README.md` — Message Routing Engine section

---

## Key Points

- **Priority-based**: Rules sorted by `priority` (lowest number = evaluated first), first match wins
- **MatchingContext fields**: `sender_id`, `chat_id`, `channel_type`, `text`, `fromMe`, `toMe`
- **Direction filtering**: `fromMe`/`toMe` can be `null` (match all), `true` (only self-sent), `false` (only others)
- **Instruction caching**: Loaded files are mtime-cached to avoid repeated disk reads
- **No match = ignored**: If no rule matches, the message is silently skipped

---

## Rule Schema

```json
{
  "id": "vip-user",
  "priority": 5,
  "sender": "1234567890",
  "recipient": "*",
  "channel": "*",
  "content_regex": "*",
  "instruction": "vip.md",
  "enabled": true,
  "fromMe": null,
  "toMe": null,
  "showSkillExec": false,
  "showErrors": true
}
```

| Field | Description |
|-------|-------------|
| `priority` | Lower = evaluated first |
| `fromMe`/`toMe` | `null` = match all, `true` = only self, `false` = only others |
| `showSkillExec` | Display tool call details in chat output |
| `showErrors` | Send error messages back to the channel |

---

## Matching Flow

```
Incoming Message
       │
       ▼
Extract MatchingContext (sender_id, chat_id, channel, text, fromMe, toMe)
       │
       ▼
Evaluate rules sorted by priority (lowest first)
  Rule → fromMe? → toMe? → sender → recipient → channel → content_regex
       │
       ▼
First match → load instruction file (mtime cached)
No match   → return (None, None) → message ignored
       │
       ▼
LLM receives specialized system prompt
```

---

## Managing Routes via Chat

Users can manage routing through WhatsApp using built-in skills:
- `"List all routing rules"` → `routing_list` skill
- `"Create a rule for 'order' → orders.md"` → `routing_add` skill
- `"Delete rule 'abc-123'"` → `routing_delete` skill

---

## Codebase

- `src/routing.py` — `RoutingEngine.match()` with `MatchCriterion` evaluation
- `src/db.py` — Persists and loads routing rules from JSON
- `skills/builtin/routing.py` — CRUD skills for route management
- `workspace/routing.json` — Runtime routing rules storage

## Related

- `concepts/react-loop.md` — What happens after routing matches
- `lookup/workspace-structure.md` — Where routing.json lives
- `lookup/built-in-skills.md` — routing_list, routing_add, routing_delete
