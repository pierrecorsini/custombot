<!-- Context: baileys/guides | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Guide: Baileys Best Practices

**Purpose**: Production-ready patterns for reliable WhatsApp integration.

## Rate Limiting

```javascript
import PQueue from 'p-queue'

const messageQueue = new PQueue({ 
  concurrency: 1, 
  interval: 1000  // 1 message/second max
})

const safeSend = (jid, content) => 
  messageQueue.add(() => sock.sendMessage(jid, content))
```

**Why**: WhatsApp bans accounts sending too many messages quickly.

## Message Store for Retry

```javascript
const messageStore = new Map()

sock.ev.on('messages.upsert', ({ messages }) => {
  for (const msg of messages) {
    if (msg.key.id) messageStore.set(msg.key.id, msg)
  }
})

// Required in socket config
const getMessage = async (key) => messageStore.get(key.id)?.message
```

**Why**: Fixes "this message can take a while" errors.

## Graceful Shutdown

```javascript
process.on('SIGINT', async () => {
  await sock.end()  // Properly close socket
  process.exit(0)
})
```

## Error Handling

```javascript
sock.ev.process(async (events) => {
  try {
    if (events['messages.upsert']) {
      // Handle messages
    }
  } catch (error) {
    logger.error({ error }, 'Event processing failed')
  }
})
```

## Auto-Reconnect Pattern

```javascript
let attempts = 0
const MAX = 5
const DELAY = 5000

if (connection === 'close' && code !== DisconnectReason.loggedOut) {
  if (++attempts <= MAX) {
    setTimeout(startSock, DELAY)
  }
} else if (connection === 'open') {
  attempts = 0  // Reset on success
}
```

## Key Warnings

| Don't | Why |
|-------|-----|
| Send bulk messages | Account ban risk |
| Send ACKs on delivery | v7.x.x banning risk |
| Use old auth folder after logout | Session corrupted |

## Reference

- https://baileys.wiki

## Related

- `guides/authentication.md`
- `lookup/baileys-disconnect-codes.md`
