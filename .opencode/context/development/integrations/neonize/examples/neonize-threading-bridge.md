<!-- Context: neonize/examples | Priority: high | Version: 1.0 | Updated: 2026-03-31 -->

# Example: Neonize Threading Bridge

**Purpose**: Bridge neonize's Go-thread callbacks to Python asyncio event loop.

---

## The Problem

neonize event handlers (`@client.event`) execute in **Go threads**, not in the asyncio event loop. You cannot call `await` or schedule async work directly inside these callbacks.

---

## Solution: asyncio.Queue Bridge

```python
import asyncio
from neonize.events import MessageEv

class NeonizeBackend:
    def __init__(self):
        self._message_queue: asyncio.Queue = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None

    # Called from Go thread — CANNOT await
    @property
    def on_message(self):
        def handler(client, ev: MessageEv):
            asyncio.run_coroutine_threadsafe(
                self._message_queue.put(ev), self._loop
            )
        return handler

    # Runs in asyncio loop — CAN await
    async def start(self, handler):
        self._loop = asyncio.get_running_loop()
        self._client.event(MessageEv)(self.on_message)
        self._client.connect()

        while self._running:
            ev = await self._message_queue.get()
            incoming = _convert_neonize_message(ev)
            await handler(incoming)
```

---

## Key Rules

1. **Never** call async code directly in Go-thread callbacks
2. **Always** use `run_coroutine_threadsafe()` to cross the boundary
3. **Store** the event loop reference at startup (`get_running_loop()`)
4. **Convert** neonize message objects to `IncomingMessage` inside the async loop

---

## Related

- `concepts/neonize-integration.md` - Architecture overview
- `src/channels/whatsapp.py` - Full NeonizeBackend implementation

**Source**: Harvested from session 2026-03-29-neonize-migration
