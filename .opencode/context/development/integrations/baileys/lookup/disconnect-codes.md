<!-- Context: baileys/lookup | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Lookup: Baileys Disconnect Reason Codes

**Purpose**: Quick reference for `lastDisconnect.error.output.statusCode` values.

## Reconnectable (Auto-retry)

| Code | Constant | Meaning | Action |
|------|----------|---------|--------|
| 428 | `connectionClosed` | Server closed connection | Reconnect |
| 408 | `connectionLost` / `timedOut` | Network issue | Reconnect |
| 515 | `restartRequired` | Server restart needed | Reconnect |
| 503 | `unavailableService` | Temp unavailable | Reconnect with delay |

## Re-auth Required

| Code | Constant | Meaning | Action |
|------|----------|---------|--------|
| 401 | `loggedOut` | Device logged out | Clear auth, re-scan QR |
| 500 | `badSession` | Corrupted session | Clear auth, re-scan QR |
| 411 | `multideviceMismatch` | Device incompatible | Check multi-device enabled |

## Manual Intervention

| Code | Constant | Meaning | Action |
|------|----------|---------|--------|
| 440 | `connectionReplaced` | Another session active | Check other devices |
| 403 | `forbidden` | Access denied | Check account status |

## Code Example

```javascript
import { DisconnectReason } from 'baileys'

const shouldReconnect = (code) => {
  const reconnectable = [
    DisconnectReason.connectionClosed,
    DisconnectReason.connectionLost,
    DisconnectReason.timedOut,
    DisconnectReason.restartRequired,
    DisconnectReason.unavailableService,
  ]
  return reconnectable.includes(code)
}

if (connection === 'close') {
  const code = lastDisconnect?.error?.output?.statusCode
  if (shouldReconnect(code)) startSock()
  else console.log('Need re-auth:', code)
}
```

## Reference

- https://baileys.wiki

## Related

- `concepts/connection-lifecycle.md`
- `guides/best-practices.md`
