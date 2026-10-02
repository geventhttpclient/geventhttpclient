"""HTTP/2 response wrapper (Sprint 3c).

The :class:`HTTP2ResponseHandle` from :mod:`geventhttpclient.http2_session`
already exposes :attr:`status_code`, :attr:`headers`, :attr:`body` and
synchronous waiting on :attr:`is_closed`. This module wraps it in a
small adapter that mirrors the ``http.client`` ergonomics used by
``HTTPResponse``:

* ``read(N)`` returns up to ``N`` bytes from the body buffer;
* ``iter_content(chunk_size)`` yields body chunks until END_STREAM;
* ``iter_lines()`` yields lines split on ``\\r\\n``;
* ``json()`` decodes the body as JSON;
* ``raise_for_status()`` raises for 4xx/5xx (matches ``http.client``).

The :class:`HTTP2Client` is a tiny convenience over ``HTTPClient``
that bundles ``submit_request`` + the wait-for-close pump with a
``HTTP2Response`` result.
"""

from __future__ import annotations

import json as stdjsonlib
from collections.abc import Iterator
from typing import Any, Self

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
        # A cursor into the handle's body buffer. read(N) returns up to
        # N bytes from here and advances it; iter_content tracks the
        # same cursor.
        self._cursor = 0

    # -- Properties ---------------------------------------------------------

    @property
    def status_code(self) -> int | None:
        return self._handle.status_code

    @property
    def headers(self) -> list[tuple[str, str]]:
        return self._handle.headers

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

    # -- Read API -----------------------------------------------------------

    def _remaining(self) -> bytes:
        body = self._handle.body
        return body[self._cursor:]

    def read(self, n: int | None = None) -> bytes:
        """Read up to ``n`` bytes. ``n=None`` reads everything.

        HTTP/2 has no chunked encoding, so we do not have to recv()
        again here — the body is fully buffered on the wire side.
        """
        remaining = self._remaining()
        if n is None:
            self._cursor = len(self._handle.body)
            return remaining
        chunk = remaining[:n]
        self._cursor += len(chunk)
        return chunk

    def iter_content(self, chunk_size: int = 4096) -> Iterator[bytes]:
        """Yield body chunks of at most ``chunk_size`` bytes.

        For h2 every DATA frame is concatenated before we get here,
        so the iteration is over a single in-memory buffer. The
        splitting semantics match the http.client ``read(N)`` contract
        for backward compatibility with the HTTPResponse API.
        """
        remaining = self._remaining()
        while remaining:
            chunk = remaining[:chunk_size]
            self._cursor += len(chunk)
            yield chunk
            remaining = self._remaining()

    def iter_lines(self, chunk_size: int = 4096) -> Iterator[bytes]:
        """Yield body lines split on ``\\r\\n``.

        The last chunk may not have a terminator if the server did
        not send one — it is yielded as-is.
        """
        buf = bytearray()
        for chunk in self.iter_content(chunk_size):
            buf.extend(chunk)
            while True:
                sep = buf.find(b"\r\n")
                if sep < 0:
                    break
                yield bytes(buf[:sep])
                del buf[:sep + 2]
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


__all__ = ["HTTP2Response", "HTTP2ResponseError"]
