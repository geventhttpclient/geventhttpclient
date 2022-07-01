# geventhttpclient

[![Build Status](https://travis-ci.org/gwik/geventhttpclient.svg?branch=master)](https://travis-ci.org/gwik/geventhttpclient)

A high performance, concurrent HTTP client library for python using 
[gevent](http://gevent.org).

`gevent.httplib` support was removed in [gevent 1.0](https://github.com/surfly/gevent/commit/b45b83b1bc4de14e3c4859362825044b8e3df7d6
), **geventhttpclient** now provides that missing functionality.

**geventhttpclient** uses a fast [http parser](https://github.com/nodejs/llhttp),
written in C.

**geventhttpclient** has been specifically designed for high concurrency,
streaming and support HTTP 1.1 persistent connections. More generally it is
designed for efficiently pulling from REST APIs and streaming APIs
like Twitter's.

Safe SSL support is provided by default. **geventhttpclient** depends on
the certifi CA Bundle. This is the same CA Bundle which ships with the
Requests codebase, and is derived from Mozilla Firefox's canonical set.

As of version 1.5, only Python 3.6+ is fully supported (with prebuilt wheels), 
but Python 2.7 and 3.5 *should* work too.

Use of SSL/TLS with python 2.7.9 is not recommended and may be broken.

A simple example:

```python
#!/usr/bin/python

from geventhttpclient import HTTPClient
from geventhttpclient.url import URL

url = URL('http://gevent.org/')

http = HTTPClient(url.host)

# issue a get request
response = http.get(url.request_uri)

# read status_code
response.status_code

# read response body
body = response.read()

# close connections
http.close()
```

## httplib compatibility and monkey patch

**geventhttpclient.httplib** module contains classes for drop in
replacement of httplib connection and response objects.
If you use httplib directly you can replace the **httplib** imports
by **geventhttpclient.httplib**.

```python
# from httplib import HTTPConnection
from geventhttpclient.httplib import HTTPConnection
```

If you use **httplib2**, **urllib** or **urllib2**; you can patch **httplib** to
use the wrappers from **geventhttpclient**.
For **httplib2**, make sure you patch before you import or the *super*
calls will fail.

```python
import geventhttpclient.httplib
geventhttpclient.httplib.patch()

import httplib2
```

## High Concurrency

HTTPClient has connection pool built in and is greenlet safe by design.
You can use the same instance among several greenlets.

```python
#!/usr/bin/env python

import gevent.pool
import json

from geventhttpclient import HTTPClient
from geventhttpclient.url import URL


# go to http://developers.facebook.com/tools/explorer and copy the access token
TOKEN = '<go to http://developers.facebook.com/tools/explorer and copy the access token>'

url = URL('https://graph.facebook.com/me/friends')
url['access_token'] = TOKEN

# setting the concurrency to 10 allow to create 10 connections and
# reuse them.
http = HTTPClient.from_url(url, concurrency=10)

response = http.get(url.request_uri)
assert response.status_code == 200

# response comply to the read protocol. It passes the stream to
# the json parser as it's being read.
data = json.load(response)['data']

def print_friend_username(http, friend_id):
    friend_url = URL('/' + str(friend_id))
    friend_url['access_token'] = TOKEN
    # the greenlet will block until a connection is available
    response = http.get(friend_url.request_uri)
    assert response.status_code == 200
    friend = json.load(response)
    if friend.has_key('username'):
        print '%s: %s' % (friend['username'], friend['name'])
    else:
        print '%s has no username.' % friend['name']

# allow to run 20 greenlet at a time, this is more than concurrency
# of the http client but isn't a problem since the client has its own
# connection pool.
pool = gevent.pool.Pool(20)
for item in data:
    friend_id = item['id']
    pool.spawn(print_friend_username, http, friend_id)

pool.join()
http.close()
```

## Streaming

**geventhttpclient** supports streaming.
Response objects have a read(N) and readline() method that read the stream
incrementally.
See *src/examples/twitter_streaming.py* for pulling twitter stream API.

Here is an example on how to download a big file chunk by chunk to save memory:

```python
#!/usr/bin/env python

from geventhttpclient import HTTPClient, URL

url = URL('http://127.0.0.1:80/100.dat')
http = HTTPClient.from_url(url)
response = http.get(url.query_string)
assert response.status_code == 200

CHUNK_SIZE = 1024 * 16 # 16KB
with open('/tmp/100.dat', 'w') as f:
    data = response.read(CHUNK_SIZE)
    while data:
        f.write(data)
        data = response.read(CHUNK_SIZE)
```

## Benchmarks

The benchmark does 1000 get requests against a local nginx server with
a concurrency of 10. See *benchmarks* folder.

- httplib2 with geventhttpclient monkey patch (*benchmarks/httplib2_patched.py*): **~2500 req/s**
- geventhttpclient.HTTPClient (*benchmarks/httpclient.py*): **~4000 req/s**

