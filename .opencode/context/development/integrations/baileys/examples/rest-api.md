<!-- Context: baileys/examples | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Example: Baileys REST API

REST endpoints exposed by the Node.js bridge server.

---

## Start Connection

```http
POST /start

Response:
{
  "status": "connecting",
  "message": "Connecting to WhatsApp..."
}
```

---

## Get Status

```http
GET /status

Response:
{
  "connected": true,
  "status": "connected",
  "message": "Connected to WhatsApp"
}
```

---

## Get QR Code

```http
GET /qr

Response:
{
  "qr": "2@abc123...",
  "status": "pairing",
  "message": "Scan this QR code with WhatsApp"
}
```

---

## Send Message

```http
POST /send
Content-Type: application/json

{
  "chat_id": "1234567890@c.us",
  "text": "Hello from bot!"
}

Response:
{
  "status": "sent",
  "message_id": "3EB0abc123",
  "timestamp": 1710931200
}
```

---

## Poll Messages

```http
GET /messages

Response:
{
  "messages": [
    {
      "id": "3EB0xyz789",
      "chat_id": "1234567890@c.us",
      "sender": "1234567890@c.us",
      "sender_name": "John",
      "text": "Hello bot!",
      "timestamp": 1710931100
    }
  ],
  "count": 1
}
```

Note: Polling clears the message queue.

---

## Stop Connection

```http
POST /stop

Response:
{
  "status": "disconnected",
  "message": "Disconnected successfully"
}
```

---

## Python Client Usage

```python
from channels.whatsapp import BaileysBackend

backend = BaileysBackend(config)
await backend.start_and_wait()
await backend.send(chat_id, text)
messages = await backend.poll_messages()
```

---

## Related

- `baileys-bridge/index.js` - Server implementation
- `channels/whatsapp.py` - BaileysBackend client
