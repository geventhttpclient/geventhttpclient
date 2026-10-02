"""Live HTTP/2 integration tests against a local nginx instance.

These tests are **not** part of CI — they require a local nginx listening
on ``127.0.0.1:8443`` with HTTP/2 enabled (ALPN ``h2``) and a
self-signed certificate. Run them with::

    nginx -c /tmp/pi/nginx/nginx.conf -p /tmp/pi/nginx/ &
    .venv/bin/python -m pytest tests/test_http2_session_live.py -v

If nginx is not reachable the entire module is skipped.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

import gevent
import gevent.socket
import gevent.ssl
import pytest

from geventhttpclient.http2_session import HTTP2ResponseHandle, HTTP2Session, HTTP2WireError

NGINX_HOST = "127.0.0.1"
NGINX_PORT = 8443
NGINX_PID_FILE = "/tmp/pi/nginx/nginx.pid"
NGINX_CONFIG = "/tmp/pi/nginx/nginx.conf"
NGINX_PREFIX = "/tmp/pi/nginx"

STARTUP_TIMEOUT = 5.0
DRIVE_TIMEOUT = 5.0


def _nginx_alive() -> bool:
    try:
        with socket.create_connection((NGINX_HOST, NGINX_PORT), timeout=0.5):
            return True
    except OSError:
        return False


# Set by :func:`_start_nginx` when *this* process spawned the daemon;
# the session finalizer only shuts down nginx it started itself, so a
# developer-run instance is left alone (review part 3, "Klein": the
# no-op teardown_module left a stray daemon bound to 8443).
_nginx_spawned_by_us = False
# Detached master handles we spawned. ``nginx -s stop`` relies on the
# pid file, which a ``daemon off;`` master does not reliably leave
# behind after a ``kill -9`` -- so we track the Popen objects and
# terminate them directly.
_nginx_processes: list[subprocess.Popen[bytes]] = []


def _stop_nginx() -> None:
    """Stop the nginx daemon only if this test session started it."""
    global _nginx_spawned_by_us
    if not _nginx_spawned_by_us:
        return
    _nginx_spawned_by_us = False
    while _nginx_processes:
        proc = _nginx_processes.pop()
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _start_nginx() -> None:
    """Start the local nginx in the background.

    Skips the test if nginx is not installed or cannot be started.
    Remember whether we spawned the daemon so the session finalizer
    can take it down again.
    """
    global _nginx_spawned_by_us
    if _nginx_alive():
        return
    if not os.path.exists(NGINX_CONFIG):
        pytest.skip(f"{NGINX_CONFIG} missing")
    # Spawn nginx detached; ``daemon on;`` would be cleaner but our
    # shared config keeps ``daemon off;`` for interactive debugging.
    try:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            ["nginx", "-c", NGINX_CONFIG, "-p", NGINX_PREFIX],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        pytest.skip("nginx binary not available")
    except subprocess.SubprocessError:
        pytest.skip("nginx failed to start")
    _nginx_spawned_by_us = True
    _nginx_processes.append(proc)

    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if _nginx_alive():
            return
        gevent.sleep(0.05)
    pytest.skip("nginx did not start in time")


def _connect_h2() -> gevent.ssl.SSLSocket:
    """Open a TLS+ALPN-negotiated connection to the test nginx."""
    ctx = gevent.ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = gevent.ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2", "http/1.1"])

    sock = gevent.socket.create_connection((NGINX_HOST, NGINX_PORT), timeout=STARTUP_TIMEOUT)
    sock = ctx.wrap_socket(sock, server_hostname="localhost")
    selected = sock.selected_alpn_protocol()
    if selected != "h2":
        sock.close()
        pytest.skip(f"server did not negotiate h2 (got {selected!r})")
    return sock


def _drive_until_closed(
    session: HTTP2Session,
    handle: HTTP2ResponseHandle,
    *,
    timeout: float = DRIVE_TIMEOUT,
) -> None:
    """Drive the session until the handle reports closed or we run out of time."""
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


@pytest.fixture(autouse=True)
def _nginx_session():
    _start_nginx()
    yield


class TestLiveRoundTrip:
    def test_get_returns_json_body(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_PORT}")
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
                "POST", "/post", f"{NGINX_HOST}:{NGINX_PORT}",
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 200
            assert b'"method":"POST"' in handle.body
        finally:
            sock.close()

    def test_concurrent_streams_over_one_connection(self) -> None:
        """Two requests share one h2 session — verifies multiplexing."""
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            h1 = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_PORT}")
            h2 = session.submit_request("GET", "/get", f"{NGINX_HOST}:{NGINX_PORT}")
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
                "GET", "/no-such-path", f"{NGINX_HOST}:{NGINX_PORT}",
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 404
        finally:
            sock.close()


""
