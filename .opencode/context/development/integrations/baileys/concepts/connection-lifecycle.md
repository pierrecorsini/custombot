<!-- Context: baileys/concepts | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Concept: Baileys Connection Lifecycle

**Core Idea**: WebSocket-based WhatsApp Web API for personal/business accounts via Linked Devices feature. Not WhatsApp Business API - connects directly to WhatsApp Web protocol.

## Key Points

- **Auth Methods**: QR code scan OR pairing code (8-digit code via phone number)
- **Session Persistence**: Store credentials in `auth/` folder using `useMultiFileAuthState()`
- **Auto-Reconnect**: Handle `DisconnectReason` codes to decide reconnect vs re-auth
- **State Machine**: `connecting` → `open` → `close` (with reason code)
- **Version Required**: Node.js 17+, ESM only (v7.0+)

## Connection States

| State | Meaning |
|-------|---------|
| `connecting` | WebSocket handshake in progress |
| `open` | Authenticated and connected |
| `close` | Disconnected (check `lastDisconnect.error`) |

## Quick Example

```javascript
const { state, saveCreds } = await useMultiFileAuthState('auth_folder')
const sock = makeWASocket({ auth: { creds: state.creds, keys: ... } })

sock.ev.on('connection.update', ({ connection, qr, lastDisconnect }) => {
  if (qr) console.log('Scan QR:', qr)
  if (connection === 'open') console.log('Connected!')
  if (connection === 'close') handleDisconnect(lastDisconnect)
})
```

## Reference

- Official: https://baileys.wiki
- GitHub: https://github.com/WhiskeySockets/Baileys

## Related

- `guides/baileys-auth.md` - Authentication setup
- `lookup/baileys-disconnect-codes.md` - Error codes
