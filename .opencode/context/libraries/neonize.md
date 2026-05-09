### Create a Basic Synchronous WhatsApp Client

Source: https://context7.com/krypton-byte/neonize/llms.txt

Initialize and connect a synchronous WhatsApp client. Set up event handlers for connection and incoming messages. Requires `neonize.client.NewClient` and `neonize.events`.

```python
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv, event

# Initialize client with session name (stored in db.sqlite3)
client = NewClient("my_bot")

# Handle successful connection
@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    print("Bot connected successfully!")

# Handle incoming messages
@client.event(MessageEv)
def on_message(client: NewClient, message: MessageEv):
    text = message.Message.conversation or message.Message.extendedTextMessage.text
    chat = message.Info.MessageSource.Chat

    if text == "ping":
        client.reply_message("pong", message)
    elif text == "hello":
        client.reply_message("Hello! How can I help you?", message)

# Connect and keep running
client.connect()
event.wait()
```

--------------------------------

### Async Client Setup and Event Handling

Source: https://github.com/krypton-byte/neonize/blob/master/README.md

Sets up an asynchronous WhatsApp client using NewAClient and defines an event handler for incoming messages. Requires Python 3.10+ for the asyncio event loop.

```python
import asyncio
from neonize.aioze.client import NewAClient
from neonize.aioze.events import MessageEv, ConnectedEv

client = NewAClient("async_bot")

@client.event(MessageEv)
async def on_message(client: NewAClient, event: MessageEv):
    if event.Message.conversation == "ping":
        await client.reply_message("pong! 🏓", event)

async def main():
    await client.connect()
    await client.idle()  # Keep receiving events

asyncio.run(main())
```

--------------------------------

### Initialize and Run a Basic WhatsApp Bot

Source: https://github.com/krypton-byte/neonize/blob/master/docs/index.md

This example demonstrates how to initialize the Neonize client, set up event handlers for connection and messages, and start the bot. It requires importing necessary classes from neonize.client and neonize.events.

```python
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv, event

# Initialize client
client = NewClient("my_bot")

@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    print("🎉 Bot connected successfully!")

@client.event(MessageEv)
def on_message(client: NewClient, event: MessageEv):
    if event.message.conversation == "hi":
        client.reply_message("Hello! 👋", event.message)

# Start the bot
client.connect()
event.wait()
```

--------------------------------

### Basic Neonize Bot Setup

Source: https://github.com/krypton-byte/neonize/blob/master/README.md

Initialize a Neonize client, define event handlers for connection and messages, and start the bot. This example shows how to connect and reply to a 'hi' message.

```python
from neonize.client import NewClient
from neonize.events import MessageEv, ConnectedEv, event

# Initialize client
client = NewClient("your_bot_name")

@client.event(ConnectedEv)
def on_connected(client: NewClient, event: ConnectedEv):
    print("🎉 Bot connected successfully!")

@client.event(MessageEv)
def on_message(client: NewClient, event: MessageEv):
    if event.message.conversation == "hi":
        client.reply_message("Hello! 👋", event.message)

# Start the bot
client.connect()
event.wait()  # Keep running
```

--------------------------------

### Multi-Session Handling (Async)

Source: https://context7.com/krypton-byte/neonize/llms.txt

Manage multiple WhatsApp accounts asynchronously using the async ClientFactory. This version uses asyncio for concurrent operations. Event handlers are defined as async functions.

```python
import asyncio
from neonize.aioze.client import ClientFactory, NewAClient
from neonize.aioze.events import ConnectedEv, MessageEv, PairStatusEv

client_factory = ClientFactory("multisession.db")

# Load existing sessions
for device in client_factory.get_all_devices():
    client_factory.new_client(device.JID)

@client_factory.event(ConnectedEv)
async def on_connected(client: NewAClient, event: ConnectedEv):
    print("Async client connected!")

@client_factory.event(MessageEv)
async def on_message(client: NewAClient, message: MessageEv):
    text = message.Message.conversation or ""
    if text == "ping":
        await client.reply_message("pong", message)

@client_factory.event(PairStatusEv)
async def on_pair_status(client: NewAClient, message: PairStatusEv):
    print(f"Logged in as {message.ID.User}")

async def main():
    await client_factory.run()       # Connect all clients
    await client_factory.idle_all()  # Keep receiving events

asyncio.run(main())
```