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

import errno
from collections.abc import Iterable

import gevent.event
import gevent.lock
import gevent.socket
import gevent.ssl

from geventhttpclient.http2._core import (
    CONNECTION_STREAM_ID,
    DataReceived,
    HeadersReceived,
    HTTP2Connection,
    Http2Event,
    InformationalResponseReceived,
    StreamClosed,
    StreamReset,
    StreamState,
    TrailerReceived,
)


class HTTP2WireError(ConnectionError):
    """Raised when the wire pump encounters a fatal socket error.

    Subclasses :class:`ConnectionError` so the ``except
    ConnectionError`` contract holds across the h1 and h2 transports
    (review K2: a peer aborting mid-stream is the most common live
    failure and used to surface as a bare ``RuntimeError``).
    """


# ---------------------------------------------------------------------------
# Response handle
# ---------------------------------------------------------------------------


class HTTP2ResponseHandle:
    """Per-stream response state.

    Lives until the stream becomes :attr:`StreamLifecycle.CLOSED`
    (clean end of both sides) or is reset via RST_STREAM.
    ``ready.wait(timeout)`` blocks until the response headers have
    arrived; ``body`` accumulates DATA frames.
    """

    __slots__ = (
        "_closed",
        "_headers_ready",
        "body_parts",
        "error_code",
        "headers",
        "informational",
        "session",
        "state",
        "status_code",
        "stream_id",
        "trailers",
    )

    def __init__(self, session: "HTTP2Session", stream_id: int) -> None:
        self.stream_id = stream_id
        self.session = session
        self.state: StreamState | None = None
        self.status_code: int | None = None
        self.headers: list[tuple[str, str]] = []
        self.body_parts: list[bytes] = []
        self.trailers: list[tuple[str, str]] = []
        # RFC 9113 §8.1.1: 1xx informational responses observed on
        # this stream (e.g. ``103 Early Hints``). Each entry is a
        # ``(status_code, headers)`` tuple so callers can correlate
        # them with the corresponding 1xx number.
        self.informational: list[tuple[int, list[tuple[str, str]]]] = []
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

    def _on_informational(
        self,
        status_code: int,
        headers: list[tuple[str, str]],
    ) -> None:
        # 1xx early hints (RFC 9113 §8.1.1) are accumulated on the
        # handle so callers can inspect them after the response is
        # closed. They do not advance the stream lifecycle.
        self.informational.append((status_code, list(headers)))

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
        # Drive/flush are serialised across greenlets. The lock lives
        # on the base class so the helper ``flush_outbound`` /
        # ``drive_once`` methods can guard the socket without knowing
        # whether the subclass extended it.
        self._drive_lock = gevent.lock.RLock()
        # Fatal-error latch: once the parser or the socket failed, the
        # wire must not be driven again. Two latches exist on purpose:
        # the C session latches for itself (``session_check_alive`` in
        # ``ext/_http2_parser.c``, guarding direct parser users), this
        # one additionally covers socket and dispatch failures the C
        # layer never sees, and owns the error taxonomy of the wire.
        self._wire_error: Exception | None = None

    @property
    def sock(self) -> gevent.socket.socket:
        """The underlying TCP/SSL socket.

        Exposed for read-only inspection (e.g. ALPN negotiation
        results) by the higher layers; do not call ``send`` or
        ``recv`` on it directly -- use :meth:`drive_once` and
        :meth:`flush_outbound` so the wire lock applies.
        """
        return self._sock

    def close_sock(self) -> None:
        """Close the underlying socket. Idempotent.

        Higher layers (``HTTP2ConnectionPool``) call this on shutdown
        and when a session needs to be discarded (e.g. the peer
        negotiated ``http/1.1`` after our h2 preface was already
        written).
        """
        try:
            self._sock.close()
        except Exception:  # noqa: BLE001,S110
            pass

    def flush_outbound(self) -> int:
        """Push all queued outbound bytes to the socket.

        Returns the number of bytes written. The HTTP/2 preface +
        initial SETTINGS frame are sent here on the first call.
        """
        self._raise_if_broken()
        with self._drive_lock:
            try:
                return self._flush_outbound_locked()
            except Exception as e:  # noqa: BLE001
                # Deliberately blind: whatever failed here, the wire is
                # broken; ``_fatal`` latches it and maps it.
                raise self._fatal(e)

    def _fatal(self, error: Exception) -> Exception:
        """Record *error* as fatal and return the exception to raise.

        The wire owns the h2 error taxonomy: everything leaving it must
        be a :class:`ConnectionError`, so that ``except ConnectionError``
        catches this transport the way it catches HTTP/1 (see
        :mod:`geventhttpclient.http2.errors`). An nghttp2 failure
        reaches us as a bare :class:`RuntimeError` and would otherwise
        escape that contract; the original type stays in the message and
        in ``__cause__``, so an internal bug is still recognisable.
        """
        self._wire_error = error
        if isinstance(error, ConnectionError):
            return error
        mapped = HTTP2WireError(
            f"HTTP/2 wire failed: {type(error).__name__}: {error}",
        )
        mapped.__cause__ = error
        return mapped

    def _raise_if_broken(self) -> None:
        """Refuse to drive a wire that already failed fatally."""
        if self._wire_error is not None:
            raise HTTP2WireError(
                "HTTP/2 wire is unusable after a fatal error",
            ) from self._wire_error

    def _flush_outbound_locked(self) -> int:
        """``flush_outbound`` body, but the lock is *already* held.

        Internal helper for callers that are inside ``drive_once``'s
        locked region -- re-acquiring the lock from there would
        block forever.
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
        would block. Every fatal failure (peer close, parser error,
        failing dispatch) surfaces as :exc:`HTTP2WireError`, a
        :class:`ConnectionError`.

        ``drive_once`` is serialised against ``flush_outbound`` via
        ``self._drive_lock``; concurrent greenlets calling into the
        same session wait on the lock instead of interleaving a recv
        and a sendall on the same underlying socket.
        """
        self._raise_if_broken()
        with self._drive_lock:
            try:
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
                self._flush_outbound_locked()
                return True
            except Exception as e:  # noqa: BLE001
                # Any failure above (parser error, dispatch error,
                # socket write error) is fatal: latch so follow-up
                # drive/flush calls fail fast instead of feeding the
                # broken session again, and map into the wire's error
                # taxonomy so callers can catch a ConnectionError.
                raise self._fatal(e)

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
        # ``_drive_lock`` is set on the base ``HTTP2Wire`` -- the
        # comment there explains why we serialise drive and flush.

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
        is reached or if a peer GOAWAY forbids a new stream. Any other
        failure surfaces as :exc:`HTTP2WireError`.
        """
        # A wire that already failed must not touch the (possibly
        # latched) session a second time.
        self._raise_if_broken()
        # ``HTTP2Connection.submit_request`` ships ``body`` through
        # ``submit_data(end_stream=True)`` internally, so we no longer
        # call ``submit_data`` again here -- doing so would either be a
        # no-op (stream already finished) or duplicate the body.
        try:
            stream_id = self._connection.submit_request(
                method,
                path,
                authority,
                headers=headers,
                scheme=scheme,
                body=body,
            )
        except RuntimeError as e:
            # ``BlockingIOError`` (the documented peer-limit signal) is an
            # :class:`OSError`, so this ``except`` cannot swallow it. A
            # bare ``RuntimeError`` comes from the C session and must not
            # leave this layer as one.
            raise self._fatal(e)
        handle = HTTP2ResponseHandle(self, stream_id)
        self._handles[stream_id] = handle
        # Push the request frames immediately so the peer can start
        # working on it before we block on the response.
        self.flush_outbound()
        return handle

    # -- Event dispatch ---------------------------------------------------

    def _dispatch(self, events: list[Http2Event]) -> None:
        for event in events:
            # Class-pattern matching narrows the event union for the
            # type checker; SETTINGS, PING, GOAWAY and WINDOW_UPDATE
            # are surfaced on the HTTP2Connection for inspection but
            # not turned into per-stream callbacks at this layer.
            match event:
                case HeadersReceived():
                    self._on_headers(event)
                case DataReceived():
                    self._on_data(event)
                case InformationalResponseReceived():
                    self._on_informational(event)
                case TrailerReceived():
                    self._on_trailer_event(event)
                case StreamReset():
                    self._on_reset(event)
                case StreamClosed():
                    self._on_closed(event)

    def _handle_for(self, stream_id: int) -> HTTP2ResponseHandle | None:
        return self._handles.get(stream_id)

    def _on_headers(self, event: HeadersReceived) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is None:
            return
        # The sans-IO layer (``HTTP2Connection._convert_event``) already
        # detects trailers and 1xx informational responses and emits
        # dedicated events for them. ``_on_headers`` therefore only
        # sees the *first* response HEADERS block per stream.
        handle._on_headers(event)

    def _on_informational(self, event: InformationalResponseReceived) -> None:
        """RFC 9113 §8.1.1: 1xx early hints are stashed on the handle
        but do *not* close the stream and do *not* become the
        ``status_code`` that callers observe."""
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_informational(event.status_code, list(event.headers))

    def _on_trailer_event(self, event: TrailerReceived) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_trailer(list(event.headers))

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
        # Drop the handle so the body buffer is collected. The
        # ``HTTP2ResponseHandle`` already received its ``on_reset``
        # notification above and set its ``_closed`` event.
        self._handles.pop(event.stream_id, None)

    def _on_closed(self, event: StreamClosed) -> None:
        if event.stream_id == CONNECTION_STREAM_ID:
            return
        handle = self._handle_for(event.stream_id)
        if handle is not None:
            handle._on_closed(event)
        self._handles.pop(event.stream_id, None)


__all__ = [
    "HTTP2ResponseHandle",
    "HTTP2Session",
    "HTTP2Wire",
    "HTTP2WireError",
]
