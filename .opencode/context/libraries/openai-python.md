### Stream Responses and Chat Completions

Source: https://context7.com/openai/openai-python/llms.txt

Demonstrates streaming Server-Sent Events (SSE) from both the Responses API and the Chat Completions API using an asynchronous client. It shows how to iterate through events and retrieve final completions.

```python
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    # Stream from Responses API
    stream = await client.responses.create(
        model="gpt-5.2",
        input="Write a haiku about clouds.",
        stream=True,
    )
    async for event in stream:
        print(event)

    # Stream Chat Completions with granular event API (context manager required)
    async with client.chat.completions.stream(
        model="gpt-4o-2024-08-06",
        messages=[{"role": "user", "content": ["Count to five slowly."]}],
    ) as stream:
        async for event in stream:
            if event.type == "content.delta":
                print(event.content, end="", flush=True)
        completion = await stream.get_final_completion()
        print(f"\nFinish reason: {completion.choices[0].finish_reason}")

asyncio.run(main())
```

--------------------------------

### Stream Chat Completions with AsyncOpenAI in Python

Source: https://github.com/openai/openai-python/blob/main/helpers.md

This Python snippet demonstrates how to use the `AsyncOpenAI` client to stream chat completions. It utilizes an `async with` context manager to ensure proper resource management and iterates through the stream to process `content.delta` events, printing new content as it arrives.

```python
from openai import AsyncOpenAI

client = AsyncOpenAI()

async with client.chat.completions.stream(
    model='gpt-4o-2024-08-06',
    messages=[...],
) as stream:
    async for event in stream:
        if event.type == 'content.delta':
            print(event.content, flush=True, end='')
```

--------------------------------

### client.chat.completions.stream

Source: https://context7.com/openai/openai-python/llms.txt

Streams chat completions with a granular event API, allowing for real-time processing of generated content.

```APIDOC
## Method: client.chat.completions.stream

### Description
This method streams chat completions, providing a granular event API for processing content as it is generated. It is typically used within an `async with` context manager.

### Parameters
- **model** (str) - Required - The ID of the chat completion model to use.
- **messages** (list) - Required - A list of message objects representing the conversation history. Each message typically has a `role` and `content`.
- **stream** (bool) - Required - Must be `True` to enable streaming.

### Returns
An asynchronous iterator over `event` objects. The `stream` object also provides a `get_final_completion()` method to retrieve the full completion once streaming is done.

### Example Usage
```python
import asyncio
from openai import AsyncOpenAI

client = AsyncOpenAI()

async def main():
    async with client.chat.completions.stream(
        model="gpt-4o-2024-08-06",
        messages=[{"role": "user", "content": "Count to five slowly."}]
    ) as stream:
        async for event in stream:
            if event.type == "content.delta":
                print(event.content, end="", flush=True)
        completion = await stream.get_final_completion()
        print(f"\nFinish reason: {completion.choices[0].finish_reason}")

asyncio.run(main())
```
```

--------------------------------

### Retrieve Final Chat Completion Object from Stream (Python)

Source: https://github.com/openai/openai-python/blob/main/helpers.md

This snippet demonstrates how to use the `get_final_completion()` helper method on a chat completion stream. It allows you to asynchronously retrieve the complete `ParsedChatCompletion` object after the stream has finished processing, providing the final accumulated message content.

```python
async with client.chat.completions.stream(...) as stream:
    ...

completion = await stream.get_final_completion()
print(completion.choices[0].message)
```

--------------------------------

### POST /chat/completions

Source: https://github.com/openai/openai-python/blob/main/api.md

Creates a chat completion for the provided messages and parameters. This endpoint generates conversational responses based on the message history and supports advanced features like tool calls and streaming.

```APIDOC
## POST /chat/completions

### Description
Creates a chat completion for the provided messages and parameters. This endpoint generates conversational responses based on the message history and supports advanced features like tool calls and streaming.

### Method
POST

### Endpoint
/chat/completions

### Response
#### Success Response (200)
- **ChatCompletion** (object) - The chat completion response object containing the assistant's message and usage information

### Available Types
- ChatCompletion
- ChatCompletionChunk
- ChatCompletionMessage
- ChatCompletionMessageParam
- ChatCompletionToolUnion
- ChatCompletionStreamOptions
- ChatCompletionReasoningEffort
```