<!-- Context: baileys/navigation | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Baileys WhatsApp Integration

WebSocket-based WhatsApp Web API for Node.js.

## Structure

```
baileys/
├── concepts/
│   ├── connection-lifecycle.md    # How connection works
│   └── bridge-pattern.md          # Node.js bridge to Python bot (NEW)
├── guides/
│   ├── authentication.md          # Setup QR/pairing auth
│   └── best-practices.md          # Rate limiting, error handling
├── examples/
│   └── rest-api.md                # Bridge REST endpoints (NEW)
└── lookup/
    ├── disconnect-codes.md        # Error codes reference
    ├── events.md                  # Event types reference
    └── methods.md                 # Socket methods reference
```

## Quick Start

1. **Install**: `npm install baileys`
2. **Auth**: Scan QR or use pairing code
3. **Listen**: Handle `connection.update` and `messages.upsert`
4. **Send**: Use `sock.sendMessage(jid, content)`

## Common Tasks

| Task | See |
|------|-----|
| Setup authentication | `guides/authentication.md` |
| Handle disconnects | `lookup/disconnect-codes.md` |
| Send messages | `lookup/methods.md` |
| Handle events | `lookup/events.md` |
| Avoid bans | `guides/best-practices.md` |

## External Reference

- Docs: https://baileys.wiki
- GitHub: https://github.com/WhiskeySockets/Baileys
