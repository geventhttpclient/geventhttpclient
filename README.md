[![GitHub Workflow CI Status](https://img.shields.io/github/actions/workflow/status/geventhttpclient/geventhttpclient/test.yml?branch=master&logo=github&style=flat)](https://github.com/geventhttpclient/geventhttpclient/actions)
[![PyPI](https://img.shields.io/pypi/v/geventhttpclient.svg?style=flat)](https://pypi.org/project/geventhttpclient/)
![Python Version from PEP 621 TOML](https://img.shields.io/python/required-version-toml?tomlFilePath=https%3A%2F%2Fraw.githubusercontent.com%2Fgeventhttpclient%2Fgeventhttpclient%2Fmaster%2Fpyproject.toml)
![PyPI - Downloads](https://img.shields.io/pypi/dm/geventhttpclient)

# geventhttpclient

A [high performance](https://github.com/geventhttpclient/geventhttpclient/blob/master/README.md#benchmarks),
concurrent HTTP client library for python using
[gevent](https://www.gevent.org).

`gevent.httplib` support for patching `http.client` was removed in
[gevent 1.0](https://github.com/surfly/gevent/commit/b45b83b1bc4de14e3c4859362825044b8e3df7d6),
`geventhttpclient` now provides that missing functionality.

`geventhttpclient` uses a fast [http parser](https://github.com/nodejs/llhttp),
written in C.

`geventhttpclient` has been specifically designed for high concurrency,
streaming and support HTTP 1.1 persistent connections. More generally it is
designed for efficiently pulling from REST APIs and streaming APIs.

Safe SSL support is provided by default. `geventhttpclient` depends on
the certifi CA Bundle. This is the same CA Bundle which ships with the
Requests codebase, and is derived from Mozilla Firefox's canonical set.

## Installation

Install the latest release from PyPI:

```
pip install geventhttpclient
```

It supports Python 3.11-3.14 and requires `gevent>=25.9`. The package
ships with wheels for common platforms and falls back to a source build
with a C compiler otherwise; it is fully type annotated (`py.typed`).

## Requests-compatible interface

Since version 2.3, `geventhttpclient` features a largely `requests`
compatible interface. It covers basic HTTP usage including cookie
management, form data encoding or decoding of compressed data,
but otherwise isn't as feature rich as the original `requests`. For
simple use-cases, it can serve as a drop-in replacement.

```python
import geventhttpclient as requests

requests.get("https://github.com").text
requests.post("http://httpbingo.org/post", data="asdfasd").json()

from geventhttpclient import Session

s = Session()
s.get("http://httpbingo.org/headers").json()
s.get("https://github.com").content
```

This interface builds on top of the lower level `HTTPClient`.

```python
from geventhttpclient import HTTPClient
from geventhttpclient.url import URL

url = URL("http://gevent.org/")
client = HTTPClient(url.host)
response = client.get(url.request_uri)
response.status_code
body = response.read()
client.close()
```

## httplib compatibility and monkey patch

`geventhttpclient.httplib` module contains classes for drop in
replacement of `http.client` connection and response objects.
If you use http.client directly you can replace the `httplib` imports
by `geventhttpclient.httplib`.

```python
# from http.client import HTTPConnection
from geventhttpclient.httplib import HTTPConnection
```

If you use `httplib2` or `urllib.request`; you can patch `httplib` to use
the wrappers from `geventhttpclient`. For `httplib2`, make sure you
patch before you import or the `super()` calls will fail.

```python
import geventhttpclient.httplib

geventhttpclient.httplib.patch()

import httplib2
```

## High Concurrency

`HTTPClient` has a connection pool built in and is greenlet safe by design.
You can use the same instance among several greenlets. It is the low
level building block of this library.

```python
import gevent.pool
import json

from geventhttpclient import HTTPClient
from geventhttpclient.url import URL


# go to https://developers.facebook.com/tools/explorer and copy the access token
TOKEN = "<MY_DEV_TOKEN>"

url = URL("https://graph.facebook.com/me/friends", params={"access_token": TOKEN})

# setting the concurrency to 10 allow to create 10 connections and
# reuse them.
client = HTTPClient.from_url(url, concurrency=10)

response = client.get(url.request_uri)
assert response.status_code == 200

# response comply to the read protocol. It passes the stream to
# the json parser as it's being read.
data = json.load(response)["data"]


def print_friend_username(client, friend_id):
    friend_url = URL(f"/{friend_id}", params={"access_token": TOKEN})
    # the greenlet will block until a connection is available
    response = client.get(friend_url.request_uri)
    assert response.status_code == 200
    friend = json.load(response)
    if "username" in friend:
        print(f"{friend['username']}: {friend['name']}")
    else:
        print(f"{friend['name']} has no username.")


# allow to run 20 greenlet at a time, this is more than concurrency
# of the http client but isn't a problem since the client has its own
# connection pool.
pool = gevent.pool.Pool(20)
for item in data:
    friend_id = item["id"]
    pool.spawn(print_friend_username, client, friend_id)

pool.join()
client.close()
```

## Streaming

Response objects read the body incrementally from the socket, so a large
response never has to be held in memory. `read(n)` returns up to `n` bytes,
`readline(sep)` returns one line. Pass `b"\n"` for line oriented payloads,
as the default separator is the one that terminates HTTP headers. Iterating a
response yields `block_size` sized chunks, not lines.

Reading a streamed endpoint line by line:

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

Downloading a big file chunk by chunk keeps memory flat:

```python
from geventhttpclient import HTTPClient, URL

url = URL("https://proof.ovh.net/files/1Mb.dat")
client = HTTPClient.from_url(url)
response = client.get(url.request_uri)
assert response.status_code == 200

CHUNK_SIZE = 1024 * 16  # 16KB
with open("1Mb.dat", "wb") as f:  # binary mode, the body is raw bytes
    data = response.read(CHUNK_SIZE)
    while data:
        f.write(data)
        data = response.read(CHUNK_SIZE)
```

See [examples/oauth2.py](https://github.com/geventhttpclient/geventhttpclient/blob/master/examples/oauth2.py)
for an OAuth 2.0 client credentials example consuming a line delimited
response while it streams.

## Chunked request bodies

Requests with `Transfer-Encoding: chunked` are supported. Add the header
explicitly to have the body chunk-encoded automatically:

```python
client = HTTPClient.from_url(url)
response = client.post(
    "/upload",
    body=data,
    headers={"Transfer-Encoding": "chunked"},
)
```

Bodies of unknown length are chunk-encoded automatically, so generators and
iterables of bytes work as request bodies as well:

```python
def generate_data():
    for i in range(100):
        yield b"some data block\n"


response = client.post("/upload", body=generate_data())
```

Chunked transfer encoding requires HTTP/1.1; a `ValueError` is raised for
HTTP/1.0 requests. A user-provided `Content-Length` header is dropped when
chunked encoding is used.

## Proxy support

`HTTPClient` can route requests through an HTTP proxy. Plain HTTP requests are
forwarded with an absolute request URI, HTTPS requests are tunneled with
`CONNECT`, optionally with `Basic` proxy authentication:

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

## Development

The `llhttp` parser is vendored as a git submodule; clone with
`--recurse-submodules` (or run `git submodule update --init`) and set up
the test environment with:

```
uv sync --extra dev
uv run pytest                # gevent-monkey-patched (default)
NON_GEVENT=1 uv run pytest   # without patching (see issue #241)
```

## Benchmarks

The benchmark runs 10000 `GET` requests against a local nginx server in the
default configuration with a concurrency of 10. The requests per second for
a couple of popular clients is given in the table below. Please read
[benchmarks/README.md](https://github.com/geventhttpclient/geventhttpclient/blob/master/benchmarks/README.md)
for more details. Note that this setup is client-CPU-bound (the loopback
server answers in microseconds): it compares parsing and per-request client
efficiency. Over real network connections, latency dominates and the
differences between clients largely disappear. Also note,
[HTTPX](https://www.python-httpx.org/) is better be
used with `asyncio`, not `gevent`.

| HTTP Client        | RPS    |
| ------------------ | ------ |
| GeventHTTPClient   | 5063.4 |
| Httplib2 (patched) | 1995.7 |
| Urllib3            | 1665.8 |
| Requests           | 941.2  |
| Httpx              | 780.4  |

_Linux(x86_64), Python 3.14.7, gevent 26.9.0_
