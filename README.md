[![CI](https://img.shields.io/github/actions/workflow/status/geventhttpclient/geventhttpclient/test.yml?branch=master&logo=github&style=flat)](https://github.com/geventhttpclient/geventhttpclient/actions)
[![PyPI](https://img.shields.io/pypi/v/geventhttpclient.svg?style=flat)](https://pypi.org/project/geventhttpclient/)
[![Python](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fgeventhttpclient%2Fgeventhttpclient%2Fmaster%2Fpyproject.toml)](https://pypi.org/project/geventhttpclient/)
[![Downloads](https://img.shields.io/pypi/dm/geventhttpclient)](https://pypi.org/project/geventhttpclient/)

# geventhttpclient

**A fast, gevent-native HTTP client for Python with a low-overhead C parser, and drop-in APIs for `requests`, `httpx`
and `http.client`.**

```python
import geventhttpclient as requests

requests.get("https://github.com").text
```

## Why geventhttpclient?

- **Low per-request overhead.** HTTP parsing is done by the C-based [llhttp](https://github.com/nodejs/llhttp) parser,
  which reduces client-side CPU cost. This does not make transfers faster; see [Benchmarks](#benchmarks) for what is
  measured.
- **Built for concurrency.** A greenlet-safe connection pool with HTTP/1.1 keep-alive. Share one client across thousands
  of greenlets.
- **Streaming first.** Read bodies incrementally, line by line or in chunks, and send chunked request bodies from
  generators. Memory stays flat.
- **Drop-in compatible.** Switch from `requests`, `httpx`, `http.client`, `httplib2` or `urllib` by changing an import
  or adding one patch line. See [Choose your interface](#choose-your-interface).
- **Secure by default.** SSL verification using the `certifi` CA bundle (the same one `requests` ships).
- **Modern packaging.** Python 3.11-3.14, wheels for common platforms, fully type annotated (`py.typed`).

> **What it is not:** a full replacement for `requests` or `httpx`. It covers the common cases (cookies, form data,
> compressed bodies, proxies), but is not as feature rich, and has no HTTP/2 support.

## Installation

```bash
pip install geventhttpclient
```

Requires `gevent>=25.9`. Wheels are provided for common platforms; otherwise the package builds from source and needs a
C compiler.

## Quick start

```python
from geventhttpclient import Session

with Session() as s:
    r = s.get("https://httpbingo.org/get")
    r.raise_for_status()
    print(r.json())
```

Running many requests concurrently:

```python
import gevent.pool
from geventhttpclient import HTTPClient, URL

url = URL("https://httpbingo.org/")
client = HTTPClient.from_url(url, concurrency=10)  # pool of up to 10 connections


def fetch(i):
    return client.get(f"/get?i={i}").status_code


pool = gevent.pool.Pool(50)  # more greenlets than connections is fine, they wait for a free one
print(pool.map(fetch, range(100)))
client.close()
```

## Choose your interface

`geventhttpclient` offers several entry points, depending on what your code uses today.

| You currently use...                | Switch to                                                         | Effort                      |
|-------------------------------------|-------------------------------------------------------------------|-----------------------------|
| `requests`                          | [`import geventhttpclient as requests`](#requests-compatible)     | change one import           |
| `httpx.Client`                      | [`from geventhttpclient import httpx`](#httpx-compatible)         | change one import           |
| `http.client`                       | [`geventhttpclient.httplib`](#httpclient--httplib)                | change one import           |
| `httplib2`, `urllib.request`        | [`geventhttpclient.httplib.patch()`](#httplib2-and-urllib-monkey-patching) | one line, before the import |
| Nothing yet / maximum control       | [`HTTPClient`](#low-level-httpclient)                             | native API                  |

### requests-compatible

Covers basic HTTP usage including cookie management, form data and decoding of compressed bodies.

```python
import geventhttpclient as requests

requests.get("https://github.com").text
requests.post("http://httpbingo.org/post", data="asdfasd").json()

from geventhttpclient import Session

s = Session()
s.get("http://httpbingo.org/headers").json()
s.get("https://github.com").content
```

### httpx-compatible

Same engine and gevent concurrency, with lower HTTP parsing overhead than the `httpx` default.

```python
from geventhttpclient import httpx

with httpx.Client() as client:
    response = client.get("https://github.com")
    response.raise_for_status()
    for chunk in response.iter_text(64):
        ...
```

Limitations: per-request options are not plumbed through (the engine is configured at session level); HTTP/2 and mounts
are out of scope.

### http.client / httplib

`geventhttpclient.httplib` contains drop-in replacements for the `http.client` connection and response classes:

```python
# from http.client import HTTPConnection
from geventhttpclient.httplib import HTTPConnection
```

### httplib2 and urllib monkey patching

Libraries built on `http.client` (`httplib2`, `urllib.request`) can be patched to use the `geventhttpclient` wrappers.
For `httplib2`, patch **before** importing it, otherwise its `super()` calls will fail.

```python
import geventhttpclient.httplib

geventhttpclient.httplib.patch()

import httplib2
```

> `gevent.httplib` support for patching `http.client` was removed in
> [gevent 1.0](https://github.com/surfly/gevent/commit/b45b83b1bc4de14e3c4859362825044b8e3df7d6). `geventhttpclient`
> provides that missing functionality.

## Low-level HTTPClient

`HTTPClient` is the building block all the interfaces above are built on. It has a built-in connection pool and is
greenlet safe by design, so a single instance can be shared among many greenlets.

```python
from geventhttpclient import HTTPClient
from geventhttpclient.url import URL

url = URL("http://gevent.org/")
client = HTTPClient(url.host)
response = client.get(url.request_uri)
print(response.status_code)
body = response.read()
client.close()
```

### Concurrency

```python
import gevent.pool
from geventhttpclient import HTTPClient, URL

url = URL("https://httpbingo.org/")
client = HTTPClient.from_url(url, concurrency=10)


def fetch(i):
    response = client.get(f"/delay/1?i={i}")  # blocks until a connection is free
    return response.status_code


pool = gevent.pool.Pool(20)
results = pool.map(fetch, range(100))
client.close()
```

`concurrency` limits the number of connections in the pool. Greenlets beyond that limit wait for a free connection.

### Streaming responses

Response bodies are read incrementally from the socket, so large responses never have to be held in memory.

- `read(n)` returns up to `n` bytes.
- `readline(sep)` returns one line. Pass `b"\n"` for line-oriented payloads; the default separator is the one that
  terminates HTTP headers.
- Iterating a response yields `block_size`-sized chunks, **not** lines.

Line by line:

```python
import json
from geventhttpclient import HTTPClient, URL

url = URL("http://httpbingo.org/stream/6")
client = HTTPClient.from_url(url)
response = client.get(url.request_uri)
assert response.status_code == 200

line = response.readline(b"\n")
while line:
    print(json.loads(line)["id"])
    line = response.readline(b"\n")
```

Chunk by chunk (flat memory usage):

```python
from geventhttpclient import HTTPClient, URL

url = URL("https://proof.ovh.net/files/1Mb.dat")
client = HTTPClient.from_url(url)
response = client.get(url.request_uri)
assert response.status_code == 200

CHUNK_SIZE = 1024 * 16  # 16 KB
with open("1Mb.dat", "wb") as f:
    data = response.read(CHUNK_SIZE)
    while data:
        f.write(data)
        data = response.read(CHUNK_SIZE)
```

See [examples/oauth2.py](examples/oauth2.py) for an OAuth 2.0 client-credentials example that consumes a line-delimited
response while it streams.

### Chunked request bodies

Add the `Transfer-Encoding: chunked` header to have the body chunk-encoded automatically:

```python
from geventhttpclient import HTTPClient, URL

url = URL("http://httpbingo.org/")
client = HTTPClient.from_url(url)
data = b"some data block\n" * 100

response = client.post(
    "/post",
    body=data,
    headers={"Transfer-Encoding": "chunked"},
)
```

Bodies of unknown length are chunk-encoded automatically, so generators and iterables of bytes work too:

```python
# continuing from the snippet above
def generate_data():
    for i in range(100):
        yield b"some data block\n"


response = client.post("/post", body=generate_data())
```

Chunked encoding requires HTTP/1.1; HTTP/1.0 requests raise a `ValueError`. A user-provided `Content-Length` header is
dropped when chunked encoding is used.

### Proxy support

Plain HTTP requests are forwarded with an absolute request URI. HTTPS requests are tunneled via `CONNECT`, optionally
with `Basic` proxy authentication:

```python
client = HTTPClient(
    "target.example.com",
    port=443,
    ssl=True,
    proxy_host="proxy.example.com",
    proxy_port=3128,
    proxy_user="user",
    proxy_password="pass",
)
```

The same keyword arguments are accepted by `UserAgent` and `HTTPClientPool`.

## Benchmarks

**These numbers measure client-side CPU efficiency (HTTP parsing and per-request overhead), not transfer speed.**

The benchmark sends 10,000 `GET` requests to a local nginx in its default configuration, with concurrency 10. The
loopback server answers in microseconds, so the run is bound by the client's CPU. It therefore shows how much work each
client spends per request, and nothing about how fast data moves over a network.

| HTTP Client        | Requests/s |
|--------------------|-----------:|
| GeventHTTPClient   |       5600 |
| Httplib2 (patched) |       1990 |
| Urllib3            |       1630 |
| Requests           |        957 |
| Httpx              |        757 |

*Linux (x86_64), Python 3.14.7, gevent 26.9.0, gevent-monkey-patched*

Over real network connections, latency and bandwidth dominate, and the differences between clients largely disappear.
The lower overhead matters mainly when a client has to handle very many small requests or responses on limited CPU. Note
also that `httpx` is better used with `asyncio` than with `gevent`.

Details and instructions to reproduce: [benchmarks/README.md](benchmarks/README.md).

## Development

The `llhttp` parser is vendored as a git submodule. Clone with `--recurse-submodules` (or run
`git submodule update --init`), then:

```bash
uv sync                      # the dev group: pytest, mypy, requests, …
uv run pytest                # gevent-monkey-patched (default)
NON_GEVENT=1 uv run pytest   # without patching (see issue #241)
```

The benchmark harness needs the extra on top: `uv sync --extra benchmarks`.

See also [CHANGELOG.md](CHANGELOG.md), [RELEASING.md](RELEASING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE-MIT)
