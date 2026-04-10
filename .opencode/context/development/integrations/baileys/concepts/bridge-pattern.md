<!-- Context: baileys/concepts | Priority: high | Version: 1.0 | Updated: 2026-03-20 -->

# Concept: Baileys Bridge Pattern

**Purpose**: Connect Python bot to WhatsApp Web via a Node.js bridge server.

---

## Core Idea

A Node.js Express server runs Baileys (WhatsApp Web library) and exposes a REST API. The Python bot communicates via HTTP to send/receive messages. Session persistence via QR code scan survives restarts.

**Key Points**:
- Bridge runs as separate process (`node baileys-bridge/index.js`)
- Python uses `httpx` async client to call bridge endpoints
- Auth state persisted in `auth/` folder
- Auto-reconnects on restart if session valid

---

## Architecture

```
┌─────────────────┐     HTTP      ┌─────────────────┐
│   Python Bot    │ ←──────────→ │  Node.js Bridge │
│  (httpx client) │   REST API    │  (Express+Baileys)│
└─────────────────┘              └────────┬────────┘
                                          │
                                    ┌──────▼──────┐
                                    │  WhatsApp   │
                                    │   WebSocket │
                                    └─────────────┘
```

---

## REST Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/start` | POST | Start/restart connection |
| `/status` | GET | Connection status |
| `/qr` | GET | Get QR code for scanning |
| `/send` | POST | Send message |
| `/messages` | GET | Poll incoming messages |
| `/stop` | POST | Disconnect gracefully |

---

## Auth Flow

1. First run: Call `/start` → Call `/qr` → Display QR → User scans
2. Subsequent runs: Auto-connects using saved auth state
3. Logged out: Delete `auth/` folder, rescan

---

## Related

- `baileys-bridge/index.js` - Bridge implementation
- `channels/whatsapp.py` - BaileysBackend class
- `config.py` - BaileysConfig dataclass
