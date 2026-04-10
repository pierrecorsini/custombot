<!-- Context: baileys/lookup | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Lookup: Baileys Event Types

**Purpose**: Quick reference for `sock.ev.on()` event names.

## Connection Events

| Event | Data | When |
|-------|------|------|
| `connection.update` | `{ connection, qr, lastDisconnect }` | State change |
| `creds.update` | `AuthenticationCreds` | Credentials changed |

## Message Events

| Event | Data | When |
|-------|------|------|
| `messages.upsert` | `{ messages, type }` | New/append messages |
| `messages.update` | `[{ key, update }]` | Status change (read, delivered) |
| `messages.delete` | `{ keys }` or `{ jid, all }` | Message deleted |
| `messages.reaction` | `[{ key, reaction }]` | Reaction added |

## Chat/Contact Events

| Event | Data | When |
|-------|------|------|
| `chats.upsert` | `Chat[]` | New chats synced |
| `chats.update` | `ChatUpdate[]` | Chat metadata changed |
| `chats.delete` | `string[]` | Chats deleted |
| `contacts.upsert` | `Contact[]` | New contacts |
| `contacts.update` | `Partial<Contact>[]` | Contact changed |

## Presence Events

| Event | Data | When |
|-------|------|------|
| `presence.update` | `{ id, presences }` | Typing, online status |

## Group Events

| Event | Data | When |
|-------|------|------|
| `groups.upsert` | `GroupMetadata[]` | Group created/joined |
| `groups.update` | `Partial<GroupMetadata>[]` | Group info changed |
| `group-participants.update` | `{ id, participants, action }` | Member add/remove |

## Code Example

```javascript
sock.ev.process(async (events) => {
  if (events['connection.update']) { /* handle */ }
  if (events['messages.upsert']) { /* handle */ }
  if (events['presence.update']) { /* handle */ }
})

// Or with .on()
sock.ev.on('messages.upsert', ({ messages, type }) => {
  if (type === 'notify') { /* new message */ }
})
```

## Message Upsert Types

| Type | Meaning |
|------|---------|
| `notify` | New message requiring notification |
| `append` | Historical message being synced |

## Reference

- https://baileys.wiki

## Related

- `lookup/baileys-methods.md`
- `concepts/connection-lifecycle.md`
