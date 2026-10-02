# Changelog

API changes, deprecations and notable fixes. CI and packaging noise is
left out unless it affects users of the package.

## Unreleased

### Added

<<<<<<< HEAD
- Type annotations across the package, with a `py.typed` marker
- Basic auth added: `BasicAuth("user", "pass")`, or the `requests`-style
  `("user", "pass")` tuple, on a `Session` or a single request
- `geventhttpclient.httpx`, an httpx-compatible client on the same engine
- Requests compatibility improved: `history`, `elapsed`, `cookies`,
  `links`, `encoding`, `iter_content()`, `iter_lines()`, `json()`,
  `bool()`, `close()` and `raise_for_status` with the response attached
- `insecure` on `UserAgent`, for TLS targets without a verified chain
- `CompatRequest` derives from `urllib.request.Request`, so cookie jars
  work without workarounds
=======
- Type annotations across the whole package, with a `py.typed` marker
- `CompatRequest` derives from `urllib.request.Request` now: cookie jars
  work without workarounds, `full_url` is assignable
- `UserAgent` accepts `headers=None` when building a request
- `geventhttpclient.BasicAuth` is a first-class authentication object:
  instantiate `BasicAuth("user", "pass")` and pass it to a Session
  or a single request to set the `Authorization` header. The
  requests-style 2-tuple `auth=("user", "pass")` and a pre-built
  header value are also accepted; the requests interface adds
  session-level `Session(auth=...)` (overridable per request)

- `geventhttpclient.httpx` is an httpx-compatible drop-in surface
  on the same engine: `Client(base_url, auth, params,
follow_redirects, timeout)`, the `is_success`/`is_client_error`/.../
  `num_bytes_downloaded`/`iter_bytes`/`iter_text` Response helpers, an
  httpx-named exception hierarchy (`HTTPError`/`RequestError`/
  `HTTPStatusError`/`TooManyRedirects`/`ConnectError`/...) and
  `raise_for_status` carrying the failing request and response
- `UserAgent(..., follow_redirects=True)` and
  `raise_for_status` are also available on the requests surface:
  `response.history` collects the redirect chain (oldest first),
  `response.elapsed` is a `datetime.timedelta`, `response.cookies`
  parses the response's `Set-Cookie` headers into an
  `http.cookiejar.CookieJar`, and `raise_for_status` raises with
  the failing response and request attached; per-request `auth`
  accepts the requests-style `(username, password)` tuple and
  sets the `Authorization` header for that request
- `iter_content()`, `iter_lines()` and `json(**kw)` on `RequestsResponse`,
  mirroring the `requests` API: chunked streaming with an incremental
  unicode decoder, line splitting on `\r\n`/`\r`/`\n` across chunk
  boundaries, and `json.loads` kwargs forwarded
- `bool()`, `close()`, `is_permanent_redirect`, `encoding` and `links` on
  `RequestsResponse`
- `parse_content_type_charset()` in `geventhttpclient.header`, shared by
  `RequestsResponse.encoding` and `CompatResponse.text`
- `CompatRequest.is_unverifiable()` follows the redirect chain (RFC 2965
  section 3.3): redirected requests report unverifiable, like urllib's
  redirect handler, so strict cookie policies can refuse cookies set
  along the chain - the permissive defaults are unaffected
- Experimental HTTP/2 support (RFC 9113) backed by a vendored
  nghttp2 v1.70.0 C extension with a sans-IO core. Opt-in per client
  with `HTTPClient(..., http2=True)` or
  `UserAgent(..., http2=True)`; the default stays HTTP/1.1.
  The h2 transport negotiates ALPN and falls back to HTTP/1.1
  transparently when the peer picks `http/1.1`. 1xx informational
  responses are collected on `response.informational`, trailers on
  `response.trailers` (RFC 9113 §8.1.1 / §8.1); h2 transport errors
  raise `HTTP2Error` (a `ConnectionError` subclass) so callers can
  catch both protocol versions uniformly
>>>>>>> b81cbd5 (Rename enable_http2 to http2 and keep the h1 pool ALPN-safe)

### Changed

- Header field names and values are `str` instead of `bytes`
- Multi-value headers send one field line per element (RFC 9110)
- Redirect resolution follows RFC 3986; other schemes raise
  `UnsupportedRedirectSchemeError`
- Retries after a transmission error are limited to idempotent methods
- Empty bodies on POST, PUT and PATCH send `Content-Length: 0`
- `check_hostname`, `cert_file` and `key_file` are deprecated in favour of
  a custom SSL context; `check_hostname` now defaults to `True`
- Require `gevent>=25.9`

### Fixed

- The request head is validated and encoded as latin-1, like
  `http.client`, instead of allowing header smuggling
- `requests` and `urllib3` work again through the `httplib` shim
- Error statuses and redirects come through the httplib2 wrapper
- 307/308 redirects resend the body, and `Authorization` is dropped when
  a redirect leaves the origin
- `URL.redirect` keeps the base path and query
- `follow_redirects=False` is honoured by the `httpx.Client` shortcuts

### Removed

- `Headers.iteroriginal()` and `Headers.iget()`; use `items()` and
  `getlist()`
- The unmaintained `oauth2` extra

## 2.5.1 (2026-09-30)

### Fixed

- Requests with `body=None` sent an empty chunked body

## 2.5.0 (2026-09-28)

### Added

- Type annotations for the core modules, checked by `mypy` in CI

### Changed

- Require Python 3.11 or newer and a working `ssl` module; `urllib3` is
  no longer a dependency
- `read()` and `readline()` return `bytes` instead of `bytearray`,
  `readline()` on a released response raises `HTTPConnectionClosed`
- Header fragments decode as latin-1; the previous UTF-8 decoding
  crashed the interpreter on non-UTF-8 header bytes

### Fixed

- The body parameter of `delete()` and `trace()` works again

## 2.4.0 (2026-09-28)

### Added

- `Expect: 100-continue` handling and interim 1xx responses
- CONNECT tunneling with Basic proxy authentication via `proxy_user` and
  `proxy_password`
- Chunked transfer encoding for request bodies, used automatically for
  bodies of unknown length
- 308 redirects

### Changed

- The llhttp parser moved to v9.4.3, including the CVE-2024-27982 fix

## 2.3.9 (2026-03-03)

### Fixed

- Stale-connection validation works with the stdlib SSL implementation

## 2.3.8 (2026-02-22)

### Added

- Connection validation before reuse, so pooled connections closed by
  the server are no longer handed out

## 2.3.6 (2025-12-07)

### Changed

- Python 3.13 and 3.14 are supported explicitly

## 2.3.5 (2025-10-26)

### Fixed

- `Headers.extend()` overwrote values of duplicate fields instead of
  appending them

## 2.3.4 (2025-06-11)

### Fixed

- URL quoting for path and query components

## 2.3.3 (2024-11-24)

### Fixed

- `ssl_options` handling in the connection pool

## 2.3.1 (2024-04-18)

### Fixed

- Backwards compatibility with locust after the 2.3.0 restructuring

## 2.3.0 (2024-04-18)

### Added

- A thin `requests` compatibility layer (`geventhttpclient.requests`)
  with a `Session` speaking the requests-style API subset

## 2.2.1 (2024-04-16)

### Changed

- URL parsing is delegated to the standard library
- `max_retries` and `max_redirects` no longer count the initial attempt

## 2.2.0 (2024-04-12)

### Fixed

- `Headers` preserves the original case even for headers with duplicate
  field names
- Patching `http.client` worked only partially
