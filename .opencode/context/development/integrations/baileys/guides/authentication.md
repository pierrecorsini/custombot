<!-- Context: baileys/guides | Priority: medium | Version: 1.0 | Updated: 2026-03-20 -->

# Guide: Baileys Authentication Setup

**Purpose**: Set up QR code or pairing code authentication with session persistence.

## Prerequisites

- Node.js 17+
- ESM module (`"type": "module"` in package.json)

## Steps

### 1. Install & Import

```bash
npm install baileys pino @hapi/boom
```

```javascript
import makeWASocket, { 
  useMultiFileAuthState, 
  fetchLatestBaileysVersion,
  makeCacheableSignalKeyStore,
  DisconnectReason 
} from 'baileys'
import P from 'pino'
```

### 2. Setup Auth State

```javascript
const { state, saveCreds } = await useMultiFileAuthState('./auth')
const { version } = await fetchLatestBaileysVersion()

const sock = makeWASocket({
  version,
  logger: P({ level: 'debug' }),
  auth: {
    creds: state.creds,
    keys: makeCacheableSignalKeyStore(state.keys, logger),
  },
})
```

### 3. Handle Credential Updates

```javascript
sock.ev.on('creds.update', saveCreds)
```

### 4. Handle Connection Events

```javascript
sock.ev.on('connection.update', async ({ connection, qr, lastDisconnect }) => {
  if (qr) {
    // Display QR code (use qrcode-terminal package)
    console.log('QR:', qr)
  }
  
  if (connection === 'close') {
    const code = lastDisconnect?.error?.output?.statusCode
    if (code !== DisconnectReason.loggedOut) {
      startSock() // Reconnect
    }
  }
})
```

### 5. Pairing Code Alternative

```javascript
if (qr && !sock.authState.creds.registered) {
  const code = await sock.requestPairingCode(phoneNumber)
  console.log('Pairing code:', code) // 8-digit code
}
```

## Production Note

`useMultiFileAuthState` is for demos. Implement custom `SignalKeyStore` with Redis/DB for production.

## Reference

- https://baileys.wiki

## Related

- `concepts/connection-lifecycle.md`
- `lookup/baileys-disconnect-codes.md`
