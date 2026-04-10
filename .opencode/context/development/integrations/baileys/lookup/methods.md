<!-- Context: baileys/lookup | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Lookup: Baileys Socket Methods

**Purpose**: Quick reference for `sock.*` methods.

## Sending Messages

| Method | Params | Returns |
|--------|--------|---------|
| `sendMessage(jid, content, options?)` | jid, content object | `Promise<WAMessage>` |
| `relayMessage(jid, message, options?)` | jid, raw proto | `Promise<string>` |

### Content Types

```javascript
// Text
{ text: 'Hello', mentions?: string[] }

// Media
{ image: { url }, caption?: string }
{ video: { url }, caption?: string, gifPlayback?: boolean }
{ audio: { url }, ptt?: boolean }  // ptt=true = voice note
{ document: { url }, mimetype, fileName }

// Interactive
{ react: { key, text: '👍' } }
{ delete: messageKey }
{ location: { degreesLatitude, degreesLongitude, name } }
```

## Receipts

| Method | Purpose |
|--------|---------|
| `readMessages(keys)` | Mark as read |
| `sendReceipt(jid, participant, ids, type)` | Send delivery/read |

## Profile & Presence

| Method | Purpose |
|--------|---------|
| `profilePictureUrl(jid)` | Get avatar URL |
| `updateProfileName(name)` | Set display name |
| `updateProfileStatus(status)` | Set bio |
| `sendPresenceUpdate(type, to?)` | `composing`, `paused`, `available` |

## Groups

| Method | Purpose |
|--------|---------|
| `groupMetadata(jid)` | Get group info |
| `groupCreate(title, participants)` | Create group |
| `groupParticipantsUpdate(jid, participants, action)` | add/remove/promote/demote |
| `groupLeave(jid)` | Leave group |
| `groupInviteCode(jid)` | Get invite link |

## User Queries

| Method | Purpose |
|--------|---------|
| `onWhatsApp(jids)` | Check if numbers exist |
| `getStatus(jid)` | Get user's bio |

## Auth

| Method | Purpose |
|--------|---------|
| `requestPairingCode(phone)` | Get 8-digit code |
| `authState` | `{ creds, keys }` |
| `end()` | Close socket |

## Reference

- https://baileys.wiki

## Related

- `lookup/baileys-events.md`
- `guides/best-practices.md`
