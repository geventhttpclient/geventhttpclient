"""Tests for HTTP2ConnectionPool.

Fake-socket tests cover the pool bookkeeping (lazy creation, key reuse,
graceful close). Live nginx tests skip when no nginx is reachable on
the configured port.
"""

from __future__ import annotations

import pytest

from geventhttpclient.http2_pool import HTTP2ConnectionPool, HTTP2ConnectionPoolError

# Reuse the skip-if-no-nginx helper from the live-suite module.
from .test_session_live import (
    NGINX_H2_PORT,
    NGINX_HOST,
    _drive_until_closed,
    _require_nginx,
)


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


# ---------------------------------------------------------------------------
# Live nginx tests (skipped when no nginx is reachable)
# ---------------------------------------------------------------------------


class TestLivePool:
    def test_pool_round_trip(self) -> None:
        _require_nginx(NGINX_H2_PORT)
        pool = HTTP2ConnectionPool(insecure=True)
        try:
            session = pool.get_session(NGINX_HOST, NGINX_H2_PORT)
            handle = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
            _drive_until_closed(session, handle)
            assert handle.status_code == 200
            assert handle.body == b'{"hello":"http2","method":"GET"}'
        finally:
            pool.close()

    def test_two_requests_share_one_session(self) -> None:
        _require_nginx(NGINX_H2_PORT)
        pool = HTTP2ConnectionPool(insecure=True)
        try:
            session = pool.get_session(NGINX_HOST, NGINX_H2_PORT)
            assert pool.active_sessions() == 1

            h1 = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
            h2 = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
            _drive_until_closed(session, h1)
            _drive_until_closed(session, h2)

            assert h1.status_code == 200
            assert h2.status_code == 200
            assert h1.stream_id != h2.stream_id
            # Still only one session.
            assert pool.active_sessions() == 1
        finally:
            pool.close()

    def test_close_sends_goaway(self) -> None:
        _require_nginx(NGINX_H2_PORT)
        pool = HTTP2ConnectionPool(insecure=True)
        session = pool.get_session(NGINX_HOST, NGINX_H2_PORT)
        session.flush_outbound()
        # Drain the initial preface to ensure the session is healthy.
        h = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
        _drive_until_closed(session, h)
        assert h.status_code == 200
        # Close should submit GOAWAY and shut the socket.
        pool.close()
        assert pool.active_sessions() == 0
