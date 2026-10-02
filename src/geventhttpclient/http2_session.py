"""HTTP/2 wire session: HTTP2Connection + socket pump.

The Sprint-2 :class:`geventhttpclient.http2.HTTP2Connection` is sans-IO
and knows nothing about sockets. This module adds the I/O glue that
Sprint 3a needs:

* :class:`HTTP2Wire` — minimal ``SockIn -> HTTP2Connection -> SockOut``
  pump. The caller drives the pump; we do not spawn one for them.
* :class:`HTTP2Session` — convenience wrapper exposing
  :meth:`submit_request` (returns an :class:`HTTP2ResponseHandle`)
  and a single :meth:`drive_once` method that pumps the socket until
  it would block.

These types are intentionally small — Pool semantics, retries,
redirects and the UserAgent hook-up are Sprint 3b/3c.
"""

from __future__ import annotations

import errno
from collections.abc import Iterable

import gevent.event
import gevent.socket
import gevent.ssl

from geventhttpclient.http2 import (
    CONNECTION_STREAM_ID,
    DataReceived,
    HeadersReceived,
    HTTP2Connection,
    Http2Event,
    StreamClosed,
    StreamReset,
    StreamState,
)


class HTTP2WireError(RuntimeError):
    """Raised when the wire pump encounters a fatal socket error."""


# ---------------------------------------------------------------------------
# Response handle
# ---------------------------------------------------------------------------


class HTTP2ResponseHandle:
    """Per-stream response state.

    Lives until the stream becomes :attr:`StreamState.CLOSED` (clean
    end of both sides) or :attr:`StreamState.RESET` (RST_STREAM).
    ``ready.wait(timeout)`` blocks until the response headers have
    arrived; ``body`` accumulates DATA frames.
    """

    __slots__ = (
        "_closed",
        "_headers_ready",
        "body_parts",
        "error_code",
        "headers",
        "session",
        "state",
        "status_code",
        "stream_id",
        "trailers",
    )

    def __init__(self, session: HTTP2Session, stream_id: int) -> None:
        self.stream_id = stream_id
        self.session = session
        self.state: StreamState | None = None
        self.status_code: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body_parts: list[bytes] = []
        self.trailers: list[tuple[str, str]] = []
        self.error_code: int | None = None
        self._headers_ready = gevent.event.Event()
        self._closed = gevent.event.Event()

    def wait_ready(self, timeout: float | None = None) -> bool:
        """Block until response HEADERS have arrived."""
        return self._headers_ready.wait(timeout=timeout)

    def wait_closed(self, timeout: float | None = None) -> bool:
        """Block until the stream becomes closed (clean or reset)."""
        return self._closed.wait(timeout=timeout)

    @property
    def is_headers_ready(self) -> bool:
        return self._headers_ready.is_set()

    @property
    def is_closed(self) -> bool:
        return self._closed.is_set()

    @property
    def body(self) -> bytes:
        return b"".join(self.body_parts)

    # -- Internal event handlers used by HTTP2Session.dispatch() ----------

    def _on_headers(self, event: HeadersReceived) -> None:
        # StreamState on the connection already has the :status parsed;
        # copy the snapshot onto the handle for caller convenience.
        state = self.session.connection.get_stream(self.stream_id)
        self.state = state
        if state is not None:
            self.status_code = state.response_status_code
        self.headers = list(event.headers)
        self._headers_ready.set()

    def _on_data(self, event: DataReceived) -> None:
        if event.data:
            self.body_parts.append(bytes(event.data))

    def _on_trailer(self, trailers: list[tuple[str, str]]) -> None:
        self.trailers.extend(trailers)

    def _on_reset(self, event: StreamReset) -> None:
        self.error_code = event.error_code
        self._closed.set()

    def _on_closed(self, event: StreamClosed) -> None:
        self.error_code = event.error_code
        if not self._headers_ready.is_set():
            # Headers never arrived — surface a sensible error.
            self._headers_ready.set()
        self._closed.set()


# ---------------------------------------------------------------------------
# Wire / session
# ---------------------------------------------------------------------------


class HTTP2Wire:
    """Pump bytes from a socket through an :class:`HTTP2Connection`.

    Sans-IO design: ``drive_once`` reads at most once, then returns
    False to give the Greenlet scheduler a chance to run other
    greenlets. The caller is responsible for looping.
    """

    def __init__(
        self,
        sock: gevent.socket.socket,
        connection: HTTP2Connection,
    ) -> None:
        self._sock = sock
        self._connection = connection

    def flush_outbound(self) -> int:
        """Push all queued outbound bytes to the socket.

        Returns the number of bytes written. The HTTP/2 preface +
        initial SETTINGS frame are sent here on the first call.
        """
        data = self._connection.bytes_to_send()
        if not data:
            return 0
        self._sock.sendall(data)
        return len(data)

    def drive_once(self, max_bytes: int = 65536) -> bool:
        """Read one batch from the socket, feed it to the connection,
        and flush any outbound bytes nghttp2 emitted in response
        (ACKs, PING replies, WINDOW_UPDATE, etc.).

        Returns True if work was done (either inbound bytes were consumed
        or outbound bytes were written). Returns False if the socket
        would block. Raises :exc:`HTTP2WireError` if the peer has
        closed the socket.
        """
        try:
            inbound = self._sock.recv(max_bytes)
        except gevent.socket.timeout:
            return False
        except gevent.socket.error as e:
            if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise HTTP2WireError(str(e)) from e
        if not inbound:
            raise HTTP2WireError("peer closed the connection")

        events = self._connection.feed(inbound)
        self._dispatch(events)
        self.flush_outbound()
        return True

    # Subclasses (HTTP2Session) override to do per-event work.
    def _dispatch(self, events: list[Http2Event]) -> None:
        pass


class HTTP2Session(HTTP2Wire):
    """HTTP/2 wire session bound to a TLS-or-PLAIN socket.

    Adds :meth:`submit_request` and per-stream response handles.
    """

    def __init__(
        self,
        sock: gevent.socket.socket,
        connection: HTTP2Connection | None = None,
    ) -> None:
        if connection is None:
            connection = HTTP2Connection()
        super().__init__(sock, connection)
        self._handles: dict[int, HTTP2ResponseHandle] = {}

    @property
    def connection(self) -> HTTP2Connection:
        return self._connection

    def submit_request(
        self,
        method: str,
        path: str,
        authority: str,
        headers: Iterable[tuple[str, str]] | None = None,
        *,
        scheme: str = "https",
        body: bytes | None = None,
    ) -> HTTP2ResponseHandle:
        """Submit a single request and return a handle for the response.

        Raises :exc:`BlockingIOError` if the peer's MAX_CONCURRENT_STREAMS
        is reached or if a peer GOAWAY forbids a new stream.
        """
        stream_id = self._connection.submit_request(
            method, path, authority, headers=headers, scheme=scheme, body=body,
        )
        if body is not None:
            self._connection.submit_data(stream_id, body, end_stream=True)
        handle = HTTP2ResponseHandle(self, stream_id)
        self._handles[stream_id] = handle
        # Push the request frames immediately so the peer can start
        # working on it before we block on the response.
        self.flush_outbound()
        return handle

    # -- Event dispatch ---------------------------------------------------

    def _dispatch(self, events: list[Http2Event]) -> None:
        for event in events:
            kind = event.kind
            if kind == "headers":
                self._on_headers(event)  # type: ignore[arg-type]
            elif kind == "data":
                self._on_data(event)  # type: ignore[arg-type]
            elif kind == "stream_reset":
                self._on_reset(event)  # type: ignore[arg-type]
            elif kind == "stream_closed":
                self._on_closed(event)  # type: ignore[arg-type]
            # SETTINGS, PING, GOAWAY, WINDOW_UPDATE are surfaced on the
            # HTTP2Connection for inspection but not turned into events
            # at this layer.

    def _handle_for(self, stream_id: int) -> HTTP2ResponseHandle | None:
        return self._handles.get(stream_id)

    def _on_headers(self, event: HeadersReceived) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_headers(event)

    def _on_data(self, event: DataReceived) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_data(event)

    def _on_reset(self, event: StreamReset) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_reset(event)

    def _on_closed(self, event: StreamClosed) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_closed(event)


__all__ = [
    "HTTP2ResponseHandle",
    "HTTP2Session",
    "HTTP2Wire",
    "HTTP2WireError",
]
