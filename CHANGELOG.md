# Changelog

API changes, deprecations and notable fixes. CI and packaging noise is
left out unless it affects users of the package.

## Unreleased

### Added

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

### Changed

- Header field names and values are plain str: reads never return bytes,
  content lengths are stored as strings, and the dict level writes
  setdefault, |= and fromkeys lowercase their keys like every other write
- The TLS arguments `check_hostname`, `cert_file` and `key_file` are
  deprecated in favour of a custom SSL context. Their semantics now
  follow `http.client`: `cert_file`/`key_file` hand over a client
  certificate, and `check_hostname` defaults to `True` (it was silently
  disabled before)
- Require `gevent>=25.9`
- A list or tuple header value becomes one field line per element:
  `{'X-Multi': ['a', 'b']}` sends `X-Multi: a` and `X-Multi: b` (RFC 9110
  section 5.2) instead of the Python repr of the list; `__setitem__` and
  `update()` replace all lines of the field, `add()` and `extend()` append;
  merging one `Headers` instance into another replaces a field as a whole
  so multi line fields survive the merge
- Redirect resolution follows RFC 3986 section 5.2: dot segments are
  removed (`/a/b/c/../up` now resolves to `/a/b/up`), relative paths merge
  against all but the last base path segment (`/dir/page` + `test.html`
  resolves to `/dir/test.html`, not `/dir/page/test.html`), and a
  protocol-relative `Location: //host/path` inherits the scheme instead of
  downgrading an https connection to plain http
- Redirects to schemes other than `http`/`https` raise
  `UnsupportedRedirectSchemeError` instead of silently sending plain HTTP
  to the redirect target
- `HEAD` requests keep their method across 301/302/303 redirects, and a
  body a server sends for a HEAD response despite the protocol no longer
  breaks the client with a parse error: the response completes and the
  connection is closed
- Automatic retries after transmission errors are limited to idempotent
  methods (GET, HEAD, OPTIONS, TRACE, PUT, DELETE); POST and PATCH are no
  longer re-sent on timeout, EPIPE, ECONNRESET or empty responses. The
  client-level resend after a broken connection (ECONNRESET/EPIPE) and
  the retry when the response read fails after the request was sent in
  full are idempotent-only as well
- An empty or absent body on POST, PUT and PATCH carries a
  `Content-Length: 0` (RFC 9112 section 6.3), like curl and http.client;
  methods without body semantics send no Content-Length

### Fixed

- `requests` and `urllib3` read empty bodies and empty header dicts through
  the `httplib` shim: a response that completes within one read hands its
  socket back while the body is still buffered, which urllib3's
  `is_fp_closed()` mistook for a finished stream; `msg.items()` was a
  one-shot iterator that urllib3 exhausted while rebuilding its header
  dict, dropping e.g. `Content-Encoding` (compressed bodies arrived
  undecoded); the shim exposed an `fp` attribute that made urllib3 treat
  the already-dechunked payload as raw chunked wire format, and lacked the
  `_method` attribute its chunked reader inspects
- The httplib2 wrapper lost the response status: error statuses came
  back as 200 through it and redirects were never followed
- The request head is validated: methods, header field names, header
  values and the request URI reject CR, LF and control characters with a
  `ValueError` instead of smuggling extra headers or requests onto the
  connection, and `//host/path` targets are rejected because they are
  protocol-relative references, not origin-form paths (RFC 9112 section
  3.2)
- The request head is encoded as latin-1, matching the response header
  decoding and `http.client`; characters outside latin-1 raise a
  `UnicodeEncodeError` instead of silently sending UTF-8 bytes
- 307/308 redirects resend the full body: seekable payloads are rewound
  before the redirect is followed, payloads that cannot be rewound (also
  streams whose seek fails, like a pipe) raise `UnrewoundBodyError`
  instead of shipping an empty body under the original length
- The `Authorization` header is dropped when a redirect leaves the
  origin (scheme, host or port) of the original request
- `URL.redirect` keeps the base path - with its `;parameters` - and the
  base query for references without a path: `Location: "?x=1"` resolves
  to `/a/b?x=1`, no longer to `/a/b/?x=1`

### Removed

- `Headers.iteroriginal()` and `Headers.iget()` (deprecated with an
  announced removal in v2.3.0). Use `Headers.items()` and
  `Headers.getlist()` instead
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
