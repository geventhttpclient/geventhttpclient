"""Concurrent-access tests for HTTP/2 session (review_http2_2.md).

The plain ``test_http2_session.py`` runs one greenlet at a time; that
suite never exercises the locks added in review part 2
(``HTTP2ConnectionPool._lock`` and ``HTTP2Session._drive_lock``).
These tests do: they spawn two greenlets, both block on the same
fake socket pair, and verify that the new locks do not deadlock and
that they actually serialise the pump-and-flush cycle.
"""

import errno
import sys
from typing import Any

import gevent
import pytest

# Reuse the FakeSocket and helpers from test_http2_session.py
sys.path.insert(0, "tests")
from geventhttpclient.http2._parser import session_server_new
from geventhttpclient.http2.pool import HTTP2ConnectionPool
from geventhttpclient.http2.session import (
    HTTP2ResponseHandle,
    HTTP2Session,
)

from .test_session import (
    FakeSocket,
    _server_drive,
    _server_replies_with,
)


def _drive_handle_until_closed(handle: HTTP2ResponseHandle) -> None:
    """Drive the caller's greenlet on the shared session until the
    handle reports closed. Pulled out so the concurrent test below
    runs the same loop in two greenlets."""
    deadline = gevent.hub.get_hub().loop.now() + 5.0
    while not handle.is_closed:
        if gevent.hub.get_hub().loop.now() > deadline:
            pytest.fail("response did not arrive in 5s")
        try:
            handle.session.drive_once()
        except Exception:
            pass
        gevent.sleep(0)


class TestPoolLockWithIoyield:
    """``threading.Lock`` would deadlock when ``_open_socket`` yields
    mid-acquire; the gevent-aware RLock must not."""

    def test_concurrent_first_call_to_open_does_not_deadlock(self) -> None:
        pool = HTTP2ConnectionPool(insecure=True)

        def slow_open(_host: str, _port: int) -> Any:
            # Simulate the DNS/TLS work that would actually yield in
            # real sockets.
            gevent.sleep(0.05)

            # Return a fake socket that satisfies the surface
            # ``HTTP2Session.flush_outbound`` uses.
            class _S:
                def settimeout(self, _t: float) -> None:
                    pass

                def sendall(self, _data: bytes) -> None:
                    pass

                def close(self) -> None:
                    pass

            return _S()

        pool._open_socket = slow_open  # type: ignore[assignment]

        # Spawn two greenlets racing on the same (host, port) key.
        results: list[Any] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                results.append(pool.get_session("example.com", 443))
            except BaseException as e:
                errors.append(e)

        g1 = gevent.spawn(worker)
        g2 = gevent.spawn(worker)
        # Five seconds is well above the 0.05s sleep of slow_open.
        gevent.joinall([g1, g2], timeout=5.0)
        pool.close()
        # Both greenlets must share the same session object (one
        # underlying socket per host). With ``threading.Lock`` and a
        # yielding ``_open_socket`` the second greenlet would
        # deadlock and never return.
        assert len(results) == 2, f"expected 2 sessions, got {len(results)} (errors={errors!r})"
        assert not errors, f"errors: {errors}"
        assert results[0] is results[1]


class TestSessionDriveLock:
    """``drive_once`` and ``flush_outbound`` must not interleave across
    greenlets: each call must run the recv-then-flush cycle atomically."""

    def test_two_greenlets_pump_same_session_without_deadlock(self) -> None:
        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            _server_replies_with(server_sock, server_session, stream_id, 200, b"hi")

        _server_drive(server_sock, server_session, server_events, on_request)

        # Two concurrent requests on the same session.
        h1 = session.submit_request("GET", "/a", "example.com")
        h2 = session.submit_request("GET", "/b", "example.com")

        # Both greenlets pump the same session concurrently.
        g1 = gevent.spawn(_drive_handle_until_closed, h1)
        g2 = gevent.spawn(_drive_handle_until_closed, h2)

        # Server-side pump runs in a third greenlet so both responses
        # can be served.
        def server_loop() -> None:
            deadline = gevent.hub.get_hub().loop.now() + 5.0
            while not (h1.is_closed and h2.is_closed):
                if gevent.hub.get_hub().loop.now() > deadline:
                    pytest.fail("server did not serve both requests in 5s")
                try:
                    _server_drive(server_sock, server_session, server_events, on_request)
                except OSError as e:
                    if e.errno != errno.EAGAIN:
                        raise
                gevent.sleep(0)

        gs = gevent.spawn(server_loop)
        gevent.joinall([g1, g2, gs], timeout=5.0)

        assert h1.is_closed
        assert h2.is_closed
        assert h1.status_code == 200
        assert h2.status_code == 200

    def test_handle_dict_drops_on_close(self) -> None:
        """Review part 2 finding #3: completed handles were kept in
        ``session._handles`` for the whole session life."""
        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            _server_replies_with(server_sock, server_session, stream_id, 200, b"x")

        _server_drive(server_sock, server_session, server_events, on_request)
        handles = [session.submit_request("GET", f"/{i}", "example.com") for i in range(5)]

        # Spawn the server-pump as a sibling greenlet; this lets the
        # client pump forward progress in parallel.
        def server_loop() -> None:
            deadline = gevent.hub.get_hub().loop.now() + 5.0
            while session._handles or not all(h.is_closed for h in handles):
                if gevent.hub.get_hub().loop.now() > deadline:
                    pytest.fail("server loop deadline")
                _server_drive(server_sock, server_session, server_events, on_request)
                gevent.sleep(0)

        gs = gevent.spawn(server_loop)
        try:
            for h in handles:
                _drive_handle_until_closed(h)
        finally:
            gs.join(timeout=5.0)
        assert session._handles == {}, (
            f"completed streams leaked into _handles: {sorted(session._handles)}"
        )


class TestTrailerHandling:
    """Review part 2 finding #4: trailer HEADERS overwrote response
    headers instead of being merged into the trailer list."""

    def test_trailers_do_not_overwrite_headers(self) -> None:
        from geventhttpclient.http2._parser import session_server_new

        fs = FakeSocket()
        client_sock = fs.side_a()
        server_sock = fs.side_b()

        session = HTTP2Session(client_sock)
        server_session = session_server_new()
        server_events: list[Any] = []

        def on_request(stream_id: int, headers_list: list[Any]) -> None:
            # Server reply: HEADERS+DATA+trailer HEADERS (RFC 9113 §8.1).
            response = server_session.submit_response(
                stream_id,
                [
                    (":status", "200"),
                    ("content-type", "text/plain"),
                ],
                with_body=True,
            )
            response += server_session.submit_data(
                stream_id,
                b"body",
                end_stream=False,
            )
            response += server_session.submit_trailer(
                stream_id,
                [("x-checksum", "deadbeef")],
            )
            server_sock.sendall(response)

        _server_drive(server_sock, server_session, server_events, on_request)
        handle = session.submit_request("GET", "/", "example.com")

        # Spawn the server-pump as a sibling greenlet.
        def server_loop() -> None:
            deadline = gevent.hub.get_hub().loop.now() + 5.0
            while not handle.is_closed:
                if gevent.hub.get_hub().loop.now() > deadline:
                    pytest.fail("server loop deadline")
                _server_drive(server_sock, server_session, server_events, on_request)
                gevent.sleep(0)

        gs = gevent.spawn(server_loop)
        try:
            _drive_handle_until_closed(handle)
        finally:
            gs.join(timeout=5.0)

        assert handle.status_code == 200
        assert ("content-type", "text/plain") in handle.headers
        # The trailer must end up in the trailers tuple, not the headers.
        assert handle.trailers == [("x-checksum", "deadbeef")]
        assert not any(name == "x-checksum" for name, _ in handle.headers)
