<!-- Context: development/examples | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Example: Routing Rule Schema

JSON schema for routing rules stored in database.

---

## Schema

```json
{
  "id": "uuid-string",
  "priority": 10,
  "sender": "me",
  "recipient": "*",
  "channel": "whatsapp",
  "content_regex": "^!",
  "instruction": "commands.md",
  "enabled": true,
  "fromMe": null,
  "toMe": null,
  "showErrors": true
}
```

---

## Field Reference

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `id` | string | Unique identifier (UUID) | `"abc123..."` |
| `priority` | int | Lower = evaluated first | `10` |
| `sender` | string | Pattern for sender ID | `"me"`, `"*"`, `"1234567890"` |
| `recipient` | string | Pattern for recipient/chat | `"*"`, `"group:family"` |
| `channel` | string | Pattern for channel type | `"*"`, `"whatsapp"`, `"telegram"` |
| `content_regex` | string | Regex for message content | `"*"`, `"^!"`, `"^/cmd"` |
| `instruction` | string | Instruction filename | `"default.md"` |
| `enabled` | bool | Rule is active | `true` |
| `fromMe` | bool/null | Match messages from bot user | `true`, `false`, `null` |
| `toMe` | bool/null | Match direct messages to bot | `true`, `false`, `null` |
| `showErrors` | bool | Send error messages to channel on failure | `true`, `false` |

---

## Matching Rules

1. `*` matches any value (wildcard)
2. Plain strings match exactly
3. Regex patterns use `re.match()` (anchored at start)
4. Rules evaluated in priority order (lowest first)
5. First match wins
6. `fromMe: null` matches all messages (wildcard)
7. `fromMe: true` matches only messages from bot user
8. `fromMe: false` matches only messages from others
9. `toMe: null` matches all messages (wildcard)
10. `toMe: true` matches only direct/private messages
11. `toMe: false` matches only group messages

---

## Example Rules

```json
[
  {
    "priority": 0,
    "sender": "*",
    "recipient": "*",
    "channel": "*",
    "content_regex": "*",
    "instruction": "default.md",
    "enabled": true,
    "fromMe": null,
    "toMe": null,
    "showErrors": true
  },
  {
    "priority": 10,
    "sender": "me",
    "recipient": "*",
    "channel": "whatsapp",
    "content_regex": "*",
    "instruction": "my-messages.md",
    "enabled": true,
    "fromMe": true,
    "toMe": null,
    "showErrors": true
  },
  {
    "priority": 20,
    "sender": "*",
    "recipient": "*",
    "channel": "*",
    "content_regex": "^!",
    "instruction": "commands.md",
    "enabled": true,
    "fromMe": false,
    "toMe": true,
    "showErrors": true
  },
  {
    "priority": 30,
    "sender": "*",
    "recipient": "*",
    "channel": "whatsapp",
    "content_regex": "*",
    "instruction": "group-handler.md",
    "enabled": true,
    "fromMe": false,
    "toMe": false,
    "showErrors": false
  }
]
```

---

## Related

- `routing.py` - RoutingEngine implementation
- `db.py` - routing_rules table
