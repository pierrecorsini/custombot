<!-- Context: neonize/lookup | Priority: medium | Version: 1.0 | Updated: 2026-03-31 -->

# Lookup: Neonize API Quick Reference

**Purpose**: Quick reference for neonize Python API (ctypes → whatsmeow).

---

## Client Lifecycle

| Method | Description |
|--------|-------------|
| `NewClient(db_path)` | Create client. Session stored in SQLite at `db_path` |
| `client.connect()` | Connect to WhatsApp. Shows QR if no existing session |
| `client.is_connected` | `bool` — current connection state |
| `client.disconnect()` | Graceful disconnect |

---

## Messaging

| Method | Description |
|--------|-------------|
| `client.send_message(jid, text)` | Send text. Returns message info |
| `client.send_image(jid, image_bytes, caption)` | Send image |

---

## Events (Decorators)

| Event | When |
|-------|------|
| `@client.event(MessageEv)` | Incoming message |
| `@client.event(ConnectedEv)` | Successfully connected |
| `@client.event(DisconnectedEv)` | Connection lost |
| `@client.event(ConnectFailureEv)` | Connection failed |

---

## Session Management

- **Storage**: Single SQLite file (`db_path`)
- **First run**: QR code displayed in terminal (uses segno internally)
- **Subsequent**: Auto-reconnects using saved session
- **Reset**: Delete the SQLite file → re-pair via QR

---

## Constraints

- Python ≥3.10 (project uses 3.13 ✓)
- Callbacks run in Go threads (see threading bridge pattern)
- No built-in reconnect — handle `DisconnectedEv` manually
- WhatsApp text limit: 4000 chars (split long messages)
- Beta quality library (3 contributors, actively maintained whatsmeow core)

---

## Install

```bash
pip install neonize>=0.2.1
```

---

## Related

- `concepts/neonize-integration.md` - Architecture and threading bridge
- `src/channels/whatsapp.py` - NeonizeBackend implementation

**Source**: Harvested from session 2026-03-29-neonize-migration
