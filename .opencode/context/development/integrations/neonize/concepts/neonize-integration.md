<!-- Context: neonize/concepts | Priority: high | Version: 1.0 | Updated: 2026-03-31 -->

# Concept: Neonize Integration

**Purpose**: WhatsApp connectivity via native Python (ctypes → Go/whatsmeow), replacing the Node.js Baileys bridge.

---

## Core Idea

Neonize wraps whatsmeow (Go) as a shared library accessed via Python ctypes + protobuf. No Node.js, no HTTP bridge, no subprocess. The Go runtime spawns threads for callbacks that must be bridged to the asyncio event loop via `asyncio.run_coroutine_threadsafe()`.

---

## Key Points

- **Sync/threaded**: neonize callbacks run in Go threads, NOT in the asyncio loop
- **Bridge pattern**: `asyncio.Queue` + `run_coroutine_threadsafe()` to cross thread boundary
- **Session storage**: Single SQLite file (`whatsapp_session.db`) vs old multi-file Baileys auth
- **No built-in reconnect**: Must handle `DisconnectedEv` / `ConnectFailureEv` manually
- **Pre-compiled binaries**: Windows x86/x64/ARM64, no Go toolchain needed

---

## Architecture

```
Python (asyncio) → ctypes → Go shared lib (whatsmeow) → WhatsApp Web protocol

Event flow:
  Go thread: @client.event(MessageEv) → asyncio.run_coroutine_threadsafe(queue.put)
  Main loop: await queue.get() → _convert_message() → handler(incoming)
```

---

## Key API

| API | Description |
|-----|-------------|
| `NewClient(db_path)` | Create client with session storage path |
| `client.connect()` | Connect and show QR if no session |
| `client.send_message(jid, text)` | Send text message |
| `client.is_connected` | Connection status bool |
| `@client.event(MessageEv)` | Incoming message handler |

---

## Config

```json
{
  "whatsapp": {
    "provider": "neonize",
    "neonize": { "db_path": "workspace/whatsapp_session.db" }
  }
}
```

---

## Related

- `examples/neonize-threading-bridge.md` - Threading bridge code
- `lookup/neonize-api.md` - Full API reference
- `src/channels/whatsapp.py` - NeonizeBackend implementation

**Source**: Harvested from session 2026-03-29-neonize-migration
