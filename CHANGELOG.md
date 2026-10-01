# Changelog

API changes, deprecations and notable fixes. CI and packaging noise is
left out unless it affects users of the package.

## Unreleased

### Added

- Type annotations across the whole package, with a `py.typed` marker
- `CompatRequest` derives from `urllib.request.Request` now: cookie jars
  work without workarounds, `full_url` is assignable
- `UserAgent` accepts `headers=None` when building a request

### Changed

- The TLS arguments `check_hostname`, `cert_file` and `key_file` are
  deprecated in favour of a custom SSL context. Their semantics now
  follow `http.client`: `cert_file`/`key_file` hand over a client
  certificate, and `check_hostname` defaults to `True` (it was silently
  disabled before)
- Require `gevent>=25.9`

### Fixed

- The httplib2 wrapper lost the response status: error statuses came
  back as 200 through it and redirects were never followed

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
