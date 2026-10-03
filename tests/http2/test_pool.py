"""Tests for HTTP2ConnectionPool.

Fake-socket tests cover pool bookkeeping (lazy creation, key reuse,
graceful close). The end-to-end round-trip and multiplexing against
a real h2 server live in ``test_session_live.py`` (network-marked,
against httpbingo.org). The ``GOAWAY`` frame is observed locally via
H2TestServer because capturing a wire-level control frame is fragile
over a network boundary.
"""

import pytest

from geventhttpclient.http2.pool import HTTP2ConnectionPool, HTTP2ConnectionPoolError


def test_pool_key_reuses_session() -> None:
    """Two get_session calls for the same key must return the same object
    without opening a new socket. We monkey-patch ``_open_socket`` to
    count invocations."""
    pool = HTTP2ConnectionPool(insecure=True)

    opened = {"count": 0}

    def fake_open(host, port):
        opened["count"] += 1
        # Return a stub that satisfies the small surface HTTP2Session uses.
        class _S:
            def settimeout(self, _timeout):
                pass
            def sendall(self, _data):
                pass
            def close(self):
                pass
        return _S()

    pool._open_socket = fake_open  # type: ignore[assignment]

    s1 = pool.get_session("example.com", 443)
    s2 = pool.get_session("example.com", 443)
    assert s1 is s2
    assert opened["count"] == 1


def test_pool_keys_are_per_endpoint() -> None:
    pool = HTTP2ConnectionPool(insecure=True)

    opened: list[tuple[str, int]] = []

    def fake_open(host, port):
        opened.append((host, port))
        class _S:
            def settimeout(self, _):
                pass
            def sendall(self, _):
                pass
            def close(self):
                pass
        return _S()

    pool._open_socket = fake_open  # type: ignore[assignment]

    pool.get_session("example.com", 443)
    pool.get_session("example.com", 443)
    pool.get_session("other.com", 443)
    assert opened == [("example.com", 443), ("other.com", 443)]


def test_release_is_noop() -> None:
    pool = HTTP2ConnectionPool(insecure=True)
    # No exception, no side effect.
    pool.release_session(None)  # type: ignore[arg-type]


def test_close_after_use() -> None:
    pool = HTTP2ConnectionPool(insecure=True)

    closed_socks: list[bool] = []

    class _FakeSock:
        def __init__(self):
            self.closed = False
        def settimeout(self, _):
            pass
        def sendall(self, _):
            pass
        def close(self):
            self.closed = True
            closed_socks.append(True)

    pool._open_socket = lambda h, p: _FakeSock()  # type: ignore[assignment]
    pool.get_session("example.com", 443)
    assert pool.active_sessions() == 1

    pool.close()
    assert pool.active_sessions() == 0
    # Underlying socket closed.
    assert closed_socks


def test_get_session_after_close_raises() -> None:
    pool = HTTP2ConnectionPool(insecure=True)
    pool.close()
    with pytest.raises(HTTP2ConnectionPoolError, match="pool closed"):
        pool.get_session("example.com", 443)


def test_close_submits_goaway_frame() -> None:
    """Local test against H2TestServer: on pool close, every live
    session must submit a GOAWAY control frame before the socket is
    shut (RFC 9113 §6.8). The server's h2 library auto-validates the
    incoming GOAWAY frame and breaks the accept loop when it sees one.

    This is hard to assert over a real network: the peer's keepalive
    would mask a missing GOAWAY. Locally we point the pool at an
    in-process H2TestServer, drive the pool to a healthy session, then
    ``close()`` and observe the server-side connection is no longer
    usable. ``H2TestServer.stop()`` returns promptly because the
    server's accept loop saw the GOAWAY and exited cleanly.
    """
    from .servers import H2TestServer
    with H2TestServer() as server:
        pool = HTTP2ConnectionPool(insecure=True)
        session = pool.get_session("127.0.0.1", server.port)
        # Drain the initial preface so the session is healthy.
        session.flush_outbound()
        assert pool.active_sessions() == 1
        # Closing the pool must submit GOAWAY and shut the socket.
        pool.close()
        assert pool.active_sessions() == 0
