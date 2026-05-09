### Async Request Streaming

Source: https://github.com/encode/httpx/blob/master/docs/async.md

Details how to send streaming request bodies asynchronously using an async generator.

```APIDOC
## Async Request Streaming

### Description
When sending a request body that is streamed, such as uploading a large file, you should use an asynchronous generator with `AsyncClient`.

### Method
`AsyncClient.post(url, content=async_generator, ...)`
`AsyncClient.put(url, content=async_generator, ...)`
`AsyncClient.request(method, url, content=async_generator, ...)`

### Endpoint
N/A

### Parameters
- **url** (str) - The URL to send the request to.
- **content** (async generator) - An async generator yielding bytes for the request body.
- **method** (str) - The HTTP method for `AsyncClient.request`.

### Request Example
```python
import httpx

async def generate_bytes():
    for i in range(10):
        yield b'chunk ' + bytes(str(i), 'utf-8')

async def stream_upload():
    async with httpx.AsyncClient() as client:
        response = await client.post('https://httpbin.org/post', content=generate_bytes())
        print(response.json())
```

### Response
#### Success Response (200)
- **response** (httpx.Response) - The `Response` object containing the server's reply.

#### Response Example
```json
{
  "args": {},
  "data": "chunk 0chunk 1chunk 2chunk 3chunk 4chunk 5chunk 6chunk 7chunk 8chunk 9",
  "files": {},
  "form": {},
  "headers": {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Content-Length": "40",
    "Content-Type": "application/octet-stream",
    "Host": "httpbin.org",
    "User-Agent": "httpx/0.27.0",
    "X-Amzn-Trace-Id": "Root=1-xxxxxxxx-xxxxxxxxxxxxxxxxxxxx"
  },
  "json": null,
  "origin": "YOUR_IP_ADDRESS",
  "url": "https://httpbin.org/post"
}
```
```

--------------------------------

### Async Request Methods

Source: https://github.com/encode/httpx/blob/master/docs/async.md

Lists the available asynchronous request methods on the AsyncClient.

```APIDOC
## Async Request Methods

### Description
HTTPX provides a set of asynchronous methods on the `AsyncClient` for making various types of HTTP requests. These methods should be used with `await`.

### Method
`AsyncClient.get(url, ...)`
`AsyncClient.options(url, ...)`
`AsyncClient.head(url, ...)`
`AsyncClient.post(url, ...)`
`AsyncClient.put(url, ...)`
`AsyncClient.patch(url, ...)`
`AsyncClient.delete(url, ...)`
`AsyncClient.request(method, url, ...)`
`AsyncClient.send(request, ...)`

### Endpoint
N/A (These are methods on the `AsyncClient` instance)

### Parameters
- **url** (str) - The URL to send the request to.
- **method** (str) - The HTTP method (e.g., 'GET', 'POST') for `AsyncClient.request`.
- **request** (httpx.Request) - A `Request` object for `AsyncClient.send`.
- Other parameters like `params`, `headers`, `cookies`, `auth`, `follow_redirects`, `timeout`, `extensions`, `verify`, `cert`, `proxies`, `content`, `files`, `json` are supported as per standard HTTP request parameters.

### Request Example
```python
import httpx

async def make_request():
    async with httpx.AsyncClient() as client:
        response = await client.post('https://httpbin.org/post', json={'key': 'value'})
        print(response.json())
```

### Response
#### Success Response (200)
- **response** (httpx.Response) - The `Response` object containing the server's reply.

#### Response Example
```json
{
  "args": {},
  "data": "{\"key\": \"value\"}",
  "files": {},
  "form": {},
  "headers": {
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",
    "Content-Length": "17",
    "Content-Type": "application/json",
    "Host": "httpbin.org",
    "User-Agent": "httpx/0.27.0",
    "X-Amzn-Trace-Id": "Root=1-xxxxxxxx-xxxxxxxxxxxxxxxxxxxx"
  },
  "json": {
    "key": "value"
  },
  "origin": "YOUR_IP_ADDRESS",
  "url": "https://httpbin.org/post"
}
```
```

--------------------------------

### Configure Request Timeouts in HTTPX

Source: https://context7.com/encode/httpx/llms.txt

Demonstrates how to set global, fine-grained, and client-specific timeouts. Includes examples of handling specific timeout exceptions like ConnectTimeout and ReadTimeout.

```python
# Simple timeout
response = httpx.get('https://httpbin.org/get', timeout=10.0)

# Fine-grained timeout
timeout = httpx.Timeout(10.0, connect=30.0, read=60.0, write=60.0, pool=10.0)
response = httpx.get('https://httpbin.org/delay/5', timeout=timeout)

# Handling exceptions
try:
    response = httpx.get('https://httpbin.org/delay/10', timeout=1.0)
except httpx.TimeoutException:
    print("Timeout occurred")
```

### Timeouts > Setting and disabling timeouts

Source: https://github.com/encode/httpx/blob/master/docs/advanced/timeouts.md

You can set timeouts for an individual request using either the top-level API or a client instance. For example, to set a timeout of 10.0 seconds for a GET request, you would use `httpx.get('http://example.com/api/v1/example', timeout=10.0)` or `client.get("http://example.com/api/v1/example", timeout=10.0)` within a client instance. To disable timeouts for an individual request, you can set `timeout=None`.

--------------------------------

### Timeouts > Setting a default timeout on a client

Source: https://github.com/encode/httpx/blob/master/docs/advanced/timeouts.md

You can set a default timeout on a client instance, which will be used for all requests made with that client. This can be done when creating the client. For instance, `httpx.Client(timeout=10.0)` will use a default 10-second timeout for all requests, while `httpx.Client(timeout=None)` will disable all timeouts by default.