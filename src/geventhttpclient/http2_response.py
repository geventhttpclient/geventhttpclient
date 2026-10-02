"""HTTP/2 response wrapper.

The :class:`HTTP2ResponseHandle` from :mod:`geventhttpclient.http2_session`
already exposes :attr:`status_code`, :attr:`headers`, :attr:`body` and
synchronous waiting on :attr:`is_closed`. This module wraps it in a
small adapter that mirrors the ``http.client`` ergonomics used by
``HTTPResponse``:

* ``read(N)`` returns up to ``N`` bytes from the body buffer;
* ``iter_content(chunk_size)`` yields body chunks until END_STREAM;
* ``iter_lines()`` yields lines split on universal-newline boundaries
  (``\\r\\n``, ``\\r``, ``\\n`` — same as ``requests._iter_lines_bytes``);
* ``json()`` decodes the full body as JSON;
* ``raise_for_status()`` raises :exc:`HTTP2ResponseError` for 4xx/5xx.

:exc:`HTTP2SocketResponseBridge` is the duck-typed surface that
:class:`geventhttpclient.useragent.CompatResponse` consumes; ``request_h2``
itself still returns the raw :class:`HTTP2ResponseHandle` so callers
that only need the stream lifecycle do not have to import the bridge.
"""

import json as stdjsonlib
from collections.abc import Iterator
from typing import Any, Self

from geventhttpclient.header import Headers
from geventhttpclient.http2_session import HTTP2ResponseHandle


class HTTP2Response:
    """Read-only HTTP/2 response, sans-IO (caller has driven it closed).

    Use :meth:`read` for blocking reads or :meth:`iter_content` /
    :meth:`iter_lines` for streaming.

    The HTTP/2 spec has no chunked transfer encoding: DATA frames
    arrive as discrete units which we already concat into the
    handle's body buffer. ``read(N)`` slices that buffer and removes
    the consumed prefix.
    """

    def __init__(self, handle: HTTP2ResponseHandle) -> None:
        self._handle = handle
        # Cache the body once. ``handle.body`` rebuilds the joined
        # bytes from ``body_parts`` on every access; reading a 10 MB
        # response in 4 KB chunks would rejoin the full buffer
        # ~2500 times (O(n²)). The cached ``_body`` is computed on
        # first read, then ``_cursor`` slices it. The cache is
        # invalidated automatically when the handle receives a new
        # DATA frame (``handle.body_parts`` grew) so callers can
        # stream without reading the whole body up front.
        self._body: bytes | None = None
        self._body_seen: int = 0  # body_parts length at cache time
        self._cursor = 0
        # Header index mirrored for ``CompatResponse``-style access.
        # Uses a ``Headers`` instance (case-preserving multi-map from
        # ``header.py``) so duplicate headers like ``Set-Cookie`` are
        # not silently collapsed to a single value.
        self._headers_index = Headers()
        for name, value in handle.headers:
            self._headers_index.add(name, value)

    # -- Properties ---------------------------------------------------------

    @property
    def status_code(self) -> int | None:
        return self._handle.status_code

    @property
    def headers(self) -> list[tuple[str, str]]:
        return self._handle.headers

    @property
    def trailers(self) -> list[tuple[str, str]]:
        """Trailer section of the response (RFC 9113 §8.1).

        Empty list if the response carried no trailer HEADERS
        block. Populated by ``HTTP2Session`` after the body has
        been delivered and the stream is closed.
        """
        return self._handle.trailers

    @property
    def informational(self) -> list[tuple[int, list[tuple[str, str]]]]:
        """1xx informational responses observed on this stream.

        Each entry is ``(status_code, headers)``. Common entries
        are ``(100, ...)`` for ``100 Continue`` and ``(103, ...)``
        for ``103 Early Hints``. Empty list if the server sent no
        early hints (RFC 9113 §8.1.1).
        """
        return self._handle.informational

    @property
    def is_closed(self) -> bool:
        return self._handle.is_closed

    @property
    def content_length(self) -> int | None:
        # http.client parity: the first value of Content-Length wins.
        for name, value in self._handle.headers:
            if name.lower() == "content-length":
                try:
                    return int(value)
                except ValueError:
                    return None
        return None

    def get_code(self) -> int | None:
        """http.client parity."""
        return self.status_code

    @property
    def length(self) -> int | None:
        """http.client parity for ``__len__``."""
        return self.content_length

    @property
    def message_complete(self) -> bool:
        return self._handle.is_closed

    # -- Read API -----------------------------------------------------------

    def _body_cached(self) -> bytes:
        """Return the body joined once and cached.

        Invalidation: every read probes ``len(handle.body_parts)``
        against the snapshot taken at cache-time. If the handle has
        accumulated more DATA frames in the meantime (i.e. the
        caller streamed a partial response), the cache is rebuilt as
        ``unread remainder + newly arrived parts`` and the cursor is
        reset to 0 -- the buffer then starts exactly at the read
        position again, so ``_body[cursor:]`` stays the unread part.
        """
        seen_now = len(self._handle.body_parts)
        if self._body is None or seen_now != self._body_seen:
            new_body = b"".join(self._handle.body_parts[self._body_seen:])
            if self._body is None:
                self._body = new_body
            else:
                # Keep the unread remainder; the rebuilt buffer starts
                # at the read position, so reset the cursor.
                self._body = self._body[self._cursor:] + new_body
                self._cursor = 0
            self._body_seen = seen_now
        return self._body

    def _remaining(self) -> bytes:
        return self._body_cached()[self._cursor:]

    def read(self, n: int | None = None) -> bytes:
        """Read up to ``n`` bytes. ``n=None`` reads the remainder.

        HTTP/2 has no chunked encoding, so we do not have to recv()
        again here — the body is fully buffered on the wire side.
        Mirrors the http.client contract: a partial ``read(N)``
        advances the cursor; ``read()`` with no argument returns
        everything from the cursor to the end (review_http2_3.md M1).
        """
        body = self._body_cached()
        if n is None:
            chunk = bytes(body[self._cursor:])
            self._cursor = len(body)
            return chunk
        chunk = bytes(body[self._cursor:self._cursor + n])
        self._cursor += len(chunk)
        return chunk

    def iter_content(self, chunk_size: int = 4096) -> Iterator[bytes]:
        """Yield body chunks of at most ``chunk_size`` bytes.

        For h2 every DATA frame is concatenated before we get here,
        so the iteration is over a single in-memory buffer. The
        splitting semantics match the http.client ``read(N)`` contract
        for backward compatibility with the HTTPResponse API.
        """
        body = self._body_cached()
        total = len(body)
        while self._cursor < total:
            end = min(self._cursor + chunk_size, total)
            chunk = bytes(body[self._cursor:end])
            self._cursor = end
            yield chunk

    def iter_lines(self, chunk_size: int = 4096) -> Iterator[bytes]:
        """Yield body lines split on universal-newlines boundaries
        (``\\r\\n``, ``\\r``, or ``\\n``).

        Matches :func:`requests._iter_lines_bytes` so a drop-in for
        the http.client behaviour stays consistent across protocols.
        The last line is yielded without a terminator when the body
        does not end with one.

        Trailing-``\\r`` handling: when a chunk ends with ``\\r`` we
        hold that byte back across iterations so the next chunk can
        pair it with ``\\n`` to form a ``\\r\\n`` boundary. Without
        this guard a body of ``b"a"*4095 + b"\\r\\nb"`` (chunked at
        4096) would emit a phantom empty line — the held ``\\r`` is
        paired with the leading ``\\n`` of the next chunk (review_http2_3
        M2).
        """
        body = self._body_cached()
        # Single in-memory buffer (HTTP/2 has no chunked encoding).
        # We emulate the chunked interface so the ``\\r\\n``-over-chunk
        # test from review M2 can be reproduced.
        buf = bytearray()
        cursor = self._cursor
        total = len(body)
        while True:
            # Pull ``chunk_size`` bytes from the underlying buffer; on
            # the last iteration this may be a short read.
            end = min(cursor + chunk_size, total)
            chunk = body[cursor:end]
            buf.extend(chunk)
            cursor = end
            # Split the buffer on universal-newline boundaries.
            while True:
                crlf = buf.find(b"\r\n")
                if crlf >= 0:
                    yield bytes(buf[:crlf])
                    del buf[:crlf + 2]
                    continue
                # If the buffer ends in ``\r`` and we are NOT at end-of-body
                # we must hold it back so the next chunk can pair it
                # with a leading ``\n`` to form ``\r\n``. If we are
                # at end-of-body the trailing ``\r`` is its own line.
                if buf.endswith(b"\r") and cursor < total:
                    # Keep the trailing ``\r`` in the buffer; truncate
                    # the local view by removing the carried byte from
                    # the underlying cursor advance.
                    del buf[-1:]
                    cursor -= 1
                    break
                cr = buf.find(b"\r")
                lf = buf.find(b"\n")
                if cr >= 0 and (lf < 0 or cr <= lf):
                    yield bytes(buf[:cr])
                    del buf[:cr + 1]
                elif lf >= 0:
                    yield bytes(buf[:lf])
                    del buf[:lf + 1]
                else:
                    break
            if cursor >= total:
                break
        # Flush any leftover bytes that did not end in a terminator.
        if buf:
            yield bytes(buf)

    def json(self) -> Any:
        """Decode the body as JSON (raises :exc:`json.JSONDecodeError`)."""
        return stdjsonlib.loads(self.read())

    def raise_for_status(self) -> None:
        """Raise ``HTTPError`` for 4xx/5xx responses (http.client parity)."""
        code = self.status_code
        if code is not None and code >= 400:
            raise HTTP2ResponseError(f"HTTP {code}")

    # -- Context manager ----------------------------------------------------

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        # h2 streams auto-close; nothing to release here.
        return None


class HTTP2ResponseError(RuntimeError):
    """Raised by :meth:`HTTP2Response.raise_for_status` for 4xx/5xx."""


class HTTP2SocketResponseBridge:
    """Minimal ``HTTPSocketResponse`` duck-typed surface for an :class:`HTTP2Response`.

    Sprint 5 lets :class:`CompatResponse` wrap the h2 response
    unchanged: it exposes ``read``, ``readline``, ``release``,
    ``length``, ``_headers_index`` and ``get_code`` -- the small set
    ``useragent.CompatResponse`` actually touches.

    The underlying h2 stream auto-closes; ``release`` is a no-op.
    """

    def __init__(self, response: HTTP2Response) -> None:
        self._response = response
        # ``CompatResponse`` expects a ``Headers`` instance with
        # ``getlist`` semantics; the raw tuple list does not have
        # that.
        self._headers_index = Headers()
        for name, value in response.headers:
            self._headers_index.add(name, value)
        self._sent_request: str | None = None

    @property
    def length(self) -> int | None:
        return self._response.length

    def get_code(self) -> int | None:
        return self._response.get_code()

    @property
    def message_complete(self) -> bool:
        return self._response.message_complete

    def read(self, n: int | None = None) -> bytes:
        return self._response.read(n)

    def readline(self, sep: bytes = b"\r\n") -> bytes:
        # Compatibility shim: HTTP/2 has no chunked transfer-encoding,
        # so the body has already been concatenated by the time we get
        # here. We split on ``sep`` in the in-memory buffer and advance
        # the cursor exactly past the first match -- leaving any bytes
        # after the line for subsequent ``read()`` calls (review M3).
        buf = bytearray()
        for chunk in self._response.iter_content():
            buf.extend(chunk)
            sep_idx = buf.find(sep)
            if sep_idx >= 0:
                line = bytes(buf[:sep_idx + len(sep)])
                # Push the leftover back into the underlying buffer so
                # the next read() picks it up.
                leftover = bytes(buf[sep_idx + len(sep):])
                self._response._body = leftover + self._response._body_cached()[
                    self._response._cursor:
                ]
                self._response._cursor = 0
                return line
        return bytes(buf)

    def release(self) -> None:
        return None

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._response.iter_content())


__all__ = ["HTTP2Response", "HTTP2ResponseError", "HTTP2SocketResponseBridge"]
