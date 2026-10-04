"""Sans-IO tests for :mod:`geventhttpclient.http2.session`.

The tests wire an :class:`HTTP2Session` against a *fake* socket whose
two ends swap bytes through in-memory buffers. We then spawn a small
greenlet that pretends to be an h2 server: it reads from the fake
server-side socket, replies via the raw nghttp2 C session, and writes
the resulting frames back through the same fake socket.
"""

import errno
import threading
from collections.abc import Callable
from typing import Any

import pytest

from geventhttpclient.http2 import HTTP2Connection
from geventhttpclient.http2._parser import session_server_new
from geventhttpclient.http2.session import (
    HTTP2ResponseHandle,
    HTTP2Session,
    HTTP2Wire,
    HTTP2WireError,
)

# ---------------------------------------------------------------------------
# Fake socket
# ---------------------------------------------------------------------------


class FakeSocketEnd:
    """One end of a :class:`FakeSocket`.

    Implements the subset of :class:`gevent.socket.socket` that the
    HTTP2Wire pump uses: ``sendall``, ``recv``, ``close``.
    """

    def __init__(
        self,
        parent: "FakeSocket",
        *,
        write: bytearray,
        read: bytearray,
        is_closed: Callable[[], bool],
        close_other: Callable[[], None],
        lock: threading.Lock,
    ) -> None:
        self._parent = parent
        self._write = write
        self._read = read
        self._is_closed = is_closed
        self._close_other = close_other
        self._lock = lock
        self._closed = False

    def sendall(self, data: bytes) -> None:
        with self._lock:
            if self._closed:
                raise OSError(errno.EPIPE, "fake socket closed")
            self._write.extend(data)

    def recv(self, max_bytes: int) -> bytes:
        if self._is_closed():
            return b""
        with self._lock:
            if not self._read and self._parent.non_blocking:
                raise OSError(errno.EAGAIN, "would block")
            if not self._read:
                return b""
            chunk = bytes(self._read[:max_bytes])
            del self._read[:max_bytes]
            return chunk

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._close_other()

    def settimeout(self, timeout: float | None) -> None:
        # Not used in tests; HTTP2Wire uses gevent.socket.timeout, but
        # the FakeSocket.recv raises OSError(EAGAIN) instead.
        pass


class FakeSocket:
    """A pair of "sockets" connected to each other through bytes queues.

    ``a_to_b`` is the buffer carrying bytes written by side A and
    readable by side B, and vice versa. The lock is necessary because
    the I/O-side greenlets may interleave with the test thread.
    """

    def __init__(self) -> None:
        self.a_to_b = bytearray()
        self.b_to_a = bytearray()
        self._lock = threading.Lock()
        self._peer_closed_a = False
        self._peer_closed_b = False
        # recv raises EAGAIN instead of returning empty bytes when no
        # data is available, mimicking a non-blocking socket.
        self.non_blocking = True

    def side_a(self) -> "FakeSocketEnd":
        return FakeSocketEnd(
            self,
            write=self.a_to_b,
            read=self.b_to_a,
            is_closed=lambda: self._peer_closed_b,
            close_other=lambda: setattr(self, "_peer_closed_a", True),
            lock=self._lock,
        )

    def side_b(self) -> "FakeSocketEnd":
        return FakeSocketEnd(
            self,
            write=self.b_to_a,
            read=self.a_to_b,
            is_closed=lambda: self._peer_closed_a,
            close_other=lambda: setattr(self, "_peer_closed_b", True),
            lock=self._lock,
        )


# ---------------------------------------------------------------------------
# Server-side pump helper
# ---------------------------------------------------------------------------


def _spawn_server(sock: FakeSocketEnd, on_request: Callable[[int, list], Any]) -> None:
    """Pump ``sock`` as an h2 server.

    The on_request callback receives ``(stream_id, request_headers)``
    and is expected to send a response via the shared nghttp2
    session. We keep an unparsed-buffer dict that maps a stream_id to
    bytes the server has received so far but not yet parsed.
    """
    session = session_server_new()

    # Pull initial client bytes (preface + SETTINGS).
    sock.sendall(b"")  # noop — initial bytes already in buffer
    _pump(sock, session, [], on_request)


def _pump(
    sock: FakeSocketEnd,
    server_session: Any,
    server_events: list[Any],
    on_request: Callable[[int, list], Any],
) -> None:
    """Read once from ``sock``, feed to ``server_session``, dispatch events,
    flush auto-replies back to ``sock``. Prefer ``_server_drive`` for
    full round-trip semantics."""
    _server_drive(sock, server_session, server_events, on_request, max_rounds=1)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_fake_socket_passes_bytes_between_ends() -> None:
    fs = FakeSocket()
    a, b = fs.side_a(), fs.side_b()
    a.sendall(b"hello")
    assert b.recv(5) == b"hello"


def test_fake_socket_returns_eagain_when_empty() -> None:
    fs = FakeSocket()
    a = fs.side_a()
    with pytest.raises(OSError) as excinfo:
        a.recv(1024)
    assert excinfo.value.errno == errno.EAGAIN


def test_fake_socket_close_signals_eof() -> None:
    fs = FakeSocket()
    a, b = fs.side_a(), fs.side_b()
    a.close()
    assert b.recv(1024) == b""


# ---------------------------------------------------------------------------
# HTTP2Session round-trip with fake socket
# ---------------------------------------------------------------------------


def _server_replies_with(
    sock: FakeSocketEnd,
    server_session: Any,
    stream_id: int,
    status: int,
    body: bytes,
    content_type: str = "text/plain",
) -> None:
    response_frames = server_session.submit_response(
        stream_id,
        [(":status", str(status)), ("content-type", content_type)],
        with_body=True,
    )
    response_frames += server_session.submit_data(stream_id, body, end_stream=True)
    sock.sendall(response_frames)


def _server_drive(
    sock: FakeSocketEnd,
    server_session: Any,
    server_events: list[Any],
    on_request: Callable[[int, list], Any],
    *,
    max_rounds: int = 20,
) -> None:
    """Read all available bytes from the client and dispatch each batch.

    Loops until the client has nothing more to send AND the server has
    nothing more to reply with — this is the round-trip the tests rely
    on: it advances both sides past the handshake and the response in
    one shot.
    """
    for _ in range(max_rounds):
        progress = False
        try:
            data = sock.recv(65536)
        except OSError as e:
            if e.errno == errno.EAGAIN:
                data = b""
            else:
                raise
        if data:
            events, more = server_session.recv(data)
            server_events.extend(events)
            for event in events:
                if event.get("_kind") == "headers":
                    on_request(event["stream_id"], event["headers"])
            if more:
                # The server generated an immediate reply (SETTINGS,
                # PING-ack, response frames).
                sock.sendall(more)
                progress = True
            progress = True
        # Drain any auto-generated frames (SETTINGS ack, PING ack, etc.)
        _, more = server_session.recv(b"")
        if more:
            sock.sendall(more)
            progress = True
        if not progress:
            return


def _pump_client(
    session: HTTP2Session,
    sock: FakeSocketEnd,
    server_session: Any,
    server_events: list[Any],
    on_request: Callable[[int, list], Any],
) -> None:
    """Drive the client once (may produce outbound) and the server once."""
    try:
        session.drive_once()
    except OSError as e:
        if e.errno == errno.EAGAIN:
            pass
        else:
            raise
    _pump_client_inner(sock, server_session, server_events, on_request)


def _pump_client_inner(
    sock: FakeSocketEnd,
    server_session: Any,
    server_events: list[Any],
    on_request: Callable[[int, list], Any],
) -> None:
    _server_drive(sock, server_session, server_events, on_request)


def _wait_for_response(
    session: HTTP2Session,
    handle: HTTP2ResponseHandle,
    sock: FakeSocketEnd,
    server_session: Any,
    server_events: list[Any],
    on_request: Callable[[int, list], Any],
    *,
    max_rounds: int = 50,
) -> None:
    """Pump client/server until the response handle closes or we run
    out of rounds. The body must arrive together with the close because
    the fake server replies synchronously."""
    for _ in range(max_rounds):
        if handle.is_closed:
            # Drain anything the server pushed after close (e.g. final
            # client ACKs).
            try:
                _server_drive(sock, server_session, server_events, on_request)
            except OSError:
                pass
            return
        _pump_client(session, sock, server_session, server_events, on_request)
    raise AssertionError("response did not arrive within max_rounds")


class TestHTTP2Wire:
    def test_send_preface_and_settings(self) -> None:
        fs = FakeSocket()
        sock = fs.side_a()
        conn = HTTP2Connection()
        wire = HTTP2Wire(sock, conn)
        written = wire.flush_outbound()
        assert written > 0
        # Outbound starts with the HTTP/2 magic.
        assert bytes(fs.a_to_b).startswith(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")

    def test_drive_once_raises_on_peer_close(self) -> None:
        fs = FakeSocket()
        sock = fs.side_a()
        conn = HTTP2Connection()
        wire = HTTP2Wire(sock, conn)
        fs.side_b().close()  # peer closed
        with pytest.raises(HTTP2WireError):
            wire.drive_once()

    def test_fatal_error_latches_the_wire(self) -> None:
        """After a fatal failure (peer close, parser error, ...),
        drive/flush must fail fast instead of feeding the broken
        session again (P6)."""
        fs = FakeSocket()
        sock = fs.side_a()
        conn = HTTP2Connection()
        wire = HTTP2Wire(sock, conn)
        fs.side_b().close()
        with pytest.raises(HTTP2WireError, match="peer closed"):
            wire.drive_once()
        # Latched: follow-up calls raise immediately, naming the cause.
        with pytest.raises(HTTP2WireError, match="unusable"):
            wire.drive_once()
        with pytest.raises(HTTP2WireError, match="unusable"):
            wire.flush_outbound()

    def test_session_failure_surfaces_as_connection_error(self) -> None:
        """The *first* failure inside the nghttp2 session arrives as a
        bare ``RuntimeError`` and used to escape the wire unmapped, so
        ``except ConnectionError`` missed a peer protocol error while it
        catches the HTTP/1 equivalent. The wire must map it and latch."""
        fs = FakeSocket()
        sock = fs.side_a()
        conn = HTTP2Connection(session=session_server_new())
        wire = HTTP2Wire(sock, conn)
        fs.side_b().sendall(b"this is not the HTTP/2 client connection preface")

        with pytest.raises(ConnectionError, match="RuntimeError") as excinfo:
            wire.drive_once()
        assert isinstance(excinfo.value, HTTP2WireError)
        # The original exception stays reachable for debugging.
        assert isinstance(excinfo.value.__cause__, RuntimeError)

        # Latched through the same contract, not as a bare RuntimeError.
        with pytest.raises(HTTP2WireError, match="unusable"):
            wire.drive_once()
        # A second wire over the same (now latched) session must not
        # leak the C session's RuntimeError either.
        with pytest.raises(HTTP2WireError, match="wire failed") as second:
            HTTP2Session(sock, conn).submit_request("GET", "/", "example.com")
        assert isinstance(second.value.__cause__, RuntimeError)


class TestHTTP2SessionRoundTrip:
    def test_get_round_trip(self) -> None:
        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            body = b"hello http2"
            _server_replies_with(server_sock, server_session, stream_id, 200, body)

        # First the server must drain the client preface+SETTINGS.
        _server_drive(server_sock, server_session, server_events, on_request)

        handle = session.submit_request("GET", "/", "example.com")

        # Pump client/server until the handle reports closed.
        for _ in range(50):
            if handle.is_closed:
                break
            try:
                session.drive_once()
            except HTTP2WireError:
                pass
            _server_drive(server_sock, server_session, server_events, on_request)

        assert handle.is_closed, "response did not close after 50 rounds"
        assert handle.status_code == 200
        assert ("content-type", "text/plain") in handle.headers
        assert handle.body == b"hello http2"

    def test_post_with_body(self) -> None:
        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            body = b"the request body"
            _server_replies_with(server_sock, server_session, stream_id, 201, body)

        _server_drive(server_sock, server_session, server_events, on_request)

        handle = session.submit_request("POST", "/upload", "example.com", body=b"the request body")

        for _ in range(50):
            if handle.is_closed:
                break
            try:
                session.drive_once()
            except HTTP2WireError:
                pass
            _server_drive(server_sock, server_session, server_events, on_request)

        assert handle.is_closed, "response did not close after 50 rounds"
        assert handle.status_code == 201
        assert handle.body == b"the request body"

    def test_response_status_code_filters_pseudo_header(self) -> None:
        # :status is captured into handle.status_code and *not* in
        # handle.headers (mirrors HTTP2Connection._convert_event).
        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            _server_replies_with(server_sock, server_session, stream_id, 302, b"")

        _server_drive(server_sock, server_session, server_events, on_request)

        handle = session.submit_request("GET", "/", "example.com")

        for _ in range(50):
            if handle.is_closed:
                break
            try:
                session.drive_once()
            except HTTP2WireError:
                pass
            _server_drive(server_sock, server_session, server_events, on_request)

        assert handle.status_code == 302
        assert not any(name == ":status" for name, _ in handle.headers)

    def test_multiple_concurrent_streams(self) -> None:
        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            path = dict(headers_list).get(":path", "/")
            _server_replies_with(
                server_sock, server_session, stream_id, 200, f"body for {path}".encode()
            )

        _server_drive(server_sock, server_session, server_events, on_request)

        handles = [
            session.submit_request("GET", "/a", "example.com"),
            session.submit_request("GET", "/b", "example.com"),
        ]

        for _ in range(50):
            if all(h.is_closed for h in handles):
                break
            try:
                session.drive_once()
            except HTTP2WireError:
                pass
            _server_drive(server_sock, server_session, server_events, on_request)

        assert {h.status_code for h in handles} == {200}
        assert {h.body for h in handles} == {b"body for /a", b"body for /b"}


# ---------------------------------------------------------------------------
# Direct wire tests (pump boundary conditions)
# ---------------------------------------------------------------------------


class TestWirePump:
    def test_drive_once_after_peer_close_raises(self) -> None:
        fs = FakeSocket()
        a = fs.side_a()
        conn = HTTP2Connection()
        wire = HTTP2Wire(a, conn)
        # Flush preface so outbound queue is empty.
        wire.flush_outbound()
        fs.side_b().close()
        with pytest.raises(HTTP2WireError, match="peer closed"):
            wire.drive_once()

    def test_flush_outbound_is_idempotent(self) -> None:
        fs = FakeSocket()
        a = fs.side_a()
        conn = HTTP2Connection()
        wire = HTTP2Wire(a, conn)
        wire.flush_outbound()
        wire.flush_outbound()
        # No outbound was queued between the two calls, so the second
        # call is a no-op (returns 0).
        assert wire.flush_outbound() == 0
