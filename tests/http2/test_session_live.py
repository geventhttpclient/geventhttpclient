"""Live HTTP/2 integration tests against a local nginx instance.

These tests are **not** part of CI -- they require a local nginx already
running on the expected ports. If nothing answers on the port the
test asks for, the whole module skips cleanly via
:func:`_require_nginx`. There is no in-test nginx setup, no config
generation, no fixtures beyond the skip -- the test simply assumes
nginx is up and configured for HTTP/2 on one port and HTTP/1.1 on
another.
"""

from __future__ import annotations

import socket

import gevent
import pytest

from geventhttpclient.http2_session import HTTP2ResponseHandle, HTTP2Session, HTTP2WireError

NGINX_HOST = "127.0.0.1"
# HTTP/2-capable listener. The server's SSL config must advertise
# ``h2`` in ALPN.
NGINX_H2_PORT = 8443
# HTTP/1.1-only listener. Tests use this to verify the
# ``http2=True`` + h1-only-server fallback path.
NGINX_H1_PORT = 8444

DRIVE_TIMEOUT = 5.0


def _require_nginx(*ports: int) -> None:
    """Skip the test if none of the listed ports answers a TCP connect.

    The check is a single ``create_connection`` per port with a 0.5 s
    timeout, so a CI run with no nginx takes a couple of seconds at
    most and reports each affected test as skipped.
    """
    for port in ports:
        try:
            with socket.create_connection((NGINX_HOST, port), timeout=0.5):
                return
        except OSError:
            pass
    pytest.skip(f"no nginx reachable on {NGINX_HOST}:{','.join(str(p) for p in ports)}")


@pytest.fixture(autouse=True)
def _h2_alive():
    """All tests in this module need an h2-capable nginx."""
    _require_nginx(NGINX_H2_PORT)
    yield


def _connect_h2() -> gevent.ssl.SSLSocket:
    """Open a TLS+ALPN-negotiated connection to the test nginx."""
    import gevent.socket
    import gevent.ssl

    ctx = gevent.ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = gevent.ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2", "http/1.1"])

    sock = gevent.socket.create_connection((NGINX_HOST, NGINX_H2_PORT), timeout=DRIVE_TIMEOUT)
    sock = ctx.wrap_socket(sock, server_hostname="localhost")
    selected = sock.selected_alpn_protocol()
    if selected != "h2":
        sock.close()
        pytest.skip(f"server at {NGINX_HOST}:{NGINX_H2_PORT} did not negotiate h2 (got {selected!r})")
    return sock


def _drive_until_closed(
    session: HTTP2Session,
    handle: HTTP2ResponseHandle,
    *,
    timeout: float = DRIVE_TIMEOUT,
) -> None:
    """Drive the session until the handle reports closed or we run out of time."""
    import time
    start_time = time.time()
    while not handle.is_closed:
        if time.time() - start_time > timeout:
            pytest.fail(f"response did not close in {timeout}s (status={handle.status_code})")
        try:
            session.drive_once()
        except HTTP2WireError as e:
            pytest.fail(f"wire error: {e}")
        gevent.sleep(0)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestLiveRoundTrip:
    def test_get_returns_json_body(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
            _drive_until_closed(session, handle)
            assert handle.status_code == 200
            assert handle.body == b'{"hello":"http2","method":"GET"}'
        finally:
            sock.close()

    def test_post_request(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request(
                "POST", "/post", f"{NGINX_HOST}:{NGINX_H2_PORT}",
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 200
            assert b'"method":"POST"' in handle.body
        finally:
            sock.close()

    def test_concurrent_streams_over_one_connection(self) -> None:
        """Two requests share one h2 session -- verifies multiplexing."""
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            h1 = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
            h2 = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_H2_PORT}")
            # Different stream ids because HTTP/2 increments client streams by 2.
            assert h1.stream_id != h2.stream_id
            _drive_until_closed(session, h1)
            _drive_until_closed(session, h2)
            assert h1.status_code == 200
            assert h2.status_code == 200
            assert h1.body == h2.body
        finally:
            sock.close()

    def test_404_path_returns_404(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request(
                "GET", "/no-such-path", f"{NGINX_HOST}:{NGINX_H2_PORT}",
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 404
        finally:
            sock.close()
