### Making HTTP Requests with Client Session

Source: https://github.com/aio-libs/aiohttp/blob/master/docs/client_reference.md

Use a ClientSession for making HTTP requests. It supports the context manager protocol for automatic closing. Ensure you import aiohttp and asyncio.

```python3
import aiohttp
import asyncio

async def fetch(client):
    async with client.get('http://python.org') as resp:
        assert resp.status == 200
        return await resp.text()

async def main():
    async with aiohttp.ClientSession() as client:
        html = await fetch(client)
        print(html)

asyncio.run(main())
```

--------------------------------

### Basic aiohttp ClientSession GET Request (Python)

Source: https://github.com/aio-libs/aiohttp/blob/master/docs/http_request_lifecycle.md

Demonstrates a simple 'hello world' example using aiohttp.ClientSession to perform an asynchronous GET request and retrieve the HTML content of a URL. It utilizes `async with` for proper session and response management.

```python
import aiohttp
import asyncio

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.get('http://python.org') as response:
            html = await response.text()
            print(html)

asyncio.run(main())
```

--------------------------------

### Async Request Method

Source: https://github.com/aio-libs/aiohttp/blob/master/docs/client_reference.md

The `request` method performs an asynchronous HTTP request and returns a response object that should be used as an async context manager.

```APIDOC
## async request(method, url, params=None, data=None, json=None, cookies=None, headers=None, skip_auto_headers=None, auth=None, allow_redirects=True, max_redirects=10, compress=None, chunked=None, expect100=False, raise_for_status=None, read_until_eof=True, proxy=None, proxy_auth=None, timeout=sentinel, ssl=True, server_hostname=None, proxy_headers=None, trace_request_ctx=None, middlewares=None, read_bufsize=None, auto_decompress=None, max_line_size=None, max_field_size=None, max_headers=None)

### Description
Performs an asynchronous HTTP request. Returns a response object that should be used as an async context manager.

### Method
POST, GET, PUT, DELETE, etc. (determined by the `method` parameter)

### Endpoint
Specified by the `url` parameter

### Parameters
#### Path Parameters
None

#### Query Parameters
- **params** (Mapping, iterable of tuple of key/value pairs or str) - Optional - Parameters to be sent in the query string of the new request. Ignored for subsequent redirected requests.

#### Request Body
- **data** (FormData object, dict, bytes, or file-like object) - Optional - The data to send in the body of the request. Cannot be used with `json`.
- **json** (Any json compatible python object) - Optional - The JSON data to send in the body of the request. Cannot be used with `data`.

#### Other Parameters
- **method** (str) - Required - HTTP method.
- **url** (URL or str) - Required - Request URL.
- **cookies** (dict) - Optional - HTTP Cookies to send with the request.
- **headers** (dict) - Optional - HTTP Headers to send with the request.
- **skip_auto_headers** (Iterable of str or istr) - Optional - Set of headers for which autogeneration should be skipped.
- **auth** (aiohttp.BasicAuth) - Optional - An object that represents HTTP Basic Authorization.
- **allow_redirects** (bool) - Optional - Whether to process redirects or not. `True` by default.
- **max_redirects** (int) - Optional - Maximum number of redirects to follow. `10` by default.
- **compress** (bool) - Optional - Set to `True` if request has to be compressed with deflate encoding.
- **chunked** (int) - Optional - Enable chunked transfer encoding.
- **expect100** (bool) - Optional - Expect 100-continue response from server. `False` by default.
- **raise_for_status** (bool) - Optional - Automatically call `ClientResponse.raise_for_status()` for response if set to `True`.
- **read_until_eof** (bool) - Optional - Read response until EOF if response does not have Content-Length header. `True` by default.
- **proxy** (str or URL) - Optional - Proxy URL.
- **proxy_auth** (aiohttp.BasicAuth) - Optional - An object that represents proxy HTTP Basic Authorization.
- **timeout** (int or ClientTimeout) - Optional - Override the session's timeout.
- **ssl** (bool or aiohttp.Fingerprint) - Optional - SSL validation mode. `True` for default SSL check, `False` to skip SSL certificate validation.

### Request Example
```python
import aiohttp

async def fetch(session):
    async with session.request('GET', 'http://example.com') as response:
        return await response.text()
```

### Response
#### Success Response (200)
- **response object** - An async context manager that yields a `ClientResponse` object.

#### Response Example
```python
# Example usage within an async function
async with session.request('GET', 'http://example.com') as response:
    print(response.status)
    data = await response.json() # or response.text(), response.read(), etc.
```
```

--------------------------------

### Implement WebSocket Client

Source: https://context7.com/aio-libs/aiohttp/llms.txt

Provides an example of connecting to a WebSocket server, sending various data types (text, JSON, binary), and handling incoming messages asynchronously.

```python
import aiohttp
import asyncio

async def main():
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect('wss://echo.websocket.org') as ws:
            # Send messages
            await ws.send_str('Hello WebSocket!')
            await ws.send_json({'action': 'subscribe', 'channel': 'updates'})
            await ws.send_bytes(b'\x00\x01\x02\x03')

            # Receive messages
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    print(f"Received text: {msg.data}")
                    if msg.data == 'close':
                        await ws.close()
                        break
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    print(f"Received binary: {len(msg.data)} bytes")
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    print(f"WebSocket error: {ws.exception()}")
                    break

            print(f"WebSocket closed: {ws.close_code}")

asyncio.run(main())
```

--------------------------------

### Connect and Communicate with WebSockets

Source: https://github.com/aio-libs/aiohttp/blob/master/docs/client_quickstart.md

Use `aiohttp.ClientSession.ws_connect()` to establish a WebSocket connection. The returned `ClientWebSocketResponse` object allows sending and receiving messages. Ensure only one task handles both reading and writing to avoid issues.

```python
async with session.ws_connect('http://example.org/ws') as ws:
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            if msg.data == 'close cmd':
                await ws.close()
                break
            else:
                await ws.send_str(msg.data + '/answer')
        elif msg.type == aiohttp.WSMsgType.ERROR:
            break
```