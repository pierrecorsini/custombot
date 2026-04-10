<!-- Context: development/concepts | Priority: high | Version: 1.0 | Updated: 2026-03-20 -->

# Concept: Message Routing

**Purpose**: Route incoming messages to appropriate instruction files based on configurable rules.

---

## Core Idea

Route messages to different `.md` instruction files using 4 optional criteria. The routing engine matches rules in priority order and injects the matched instruction into the LLM system prompt.

---

## Key Points

- **4 Matching Criteria**: sender, recipient, channel, content_regex (all optional)
- **Priority Order**: Lower priority number = higher precedence
- **Wildcard Support**: `*` matches any value
- **Regex Support**: Content patterns like `^!` for commands
- **No default fallback**: Every routing rule MUST reference an existing `.md` instruction file. If missing → `sys.exit(1)` at runtime.

---

## Architecture

```
Message: {sender, recipient, channel, content}
    ↓
RoutingEngine.match(msg)
    FOR each rule (sorted by priority):
      IF all 4 criteria match → return instruction.md
    ↓
Load instruction.md → Inject into system prompt
```

---

## Rule Schema

```json
{
  "id": "uuid",
  "priority": 10,
  "sender": "me" | "*" | "1234567890" | regex,
  "recipient": "*" | "group:family" | regex,
  "channel": "*" | "whatsapp" | "telegram",
  "content_regex": "*" | "^!" | "^/cmd",
  "instruction": "default.md",
  "enabled": true,
  "fromMe": true | false | null,
  "toMe": true | false | null,
  "showErrors": true | false
}
```

---

## Example Rules

| Priority | Sender | Recipient | Channel | Regex | fromMe | toMe | showErrors | Instruction |
|----------|--------|-----------|---------|-------|--------|------|------------|-------------|
| 0 | `*` | `*` | `*` | `*` | `null` | `null` | `true` | `chat.agent.md` |
| 10 | `me` | `*` | `whatsapp` | `*` | `true` | `null` | `true` | `my-messages.md` |
| 20 | `*` | `*` | `*` | `^!` | `false` | `true` | `true` | `commands.md` |
| 30 | `*` | `*` | `whatsapp` | `*` | `false` | `false` | `false` | `group-handler.md` |

---

## Implementation

- **File**: `routing.py`
- **Class**: `RoutingEngine`
- **DB Table**: `routing_rules`
- **Skills**: `routing_add`, `routing_list`, `routing_delete`

---

## Related

- `routing.py` - Implementation
- `skills/builtin/routing.py` - CRUD skills
- `instructions/default.md` - Default instruction
