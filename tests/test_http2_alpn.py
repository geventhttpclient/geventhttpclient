"""httpx-style HTTP/2 upgrade tests (review_http2_3.md H1).

The behaviour matches httpx's ``http2=True`` kwarg:

* ``HTTPClient(enable_http2=False)`` (the default) keeps the existing
  HTTP/1.1 path untouched.
* ``HTTPClient(enable_http2=True)`` opens the h2 pool for any https://
  URL. The h2 transport negotiates ALPN; when the peer chose
  ``http/1.1`` (or did not negotiate ALPN) we close the h2 socket
  and retry on the HTTP/1.1 pool. Callers do not pass a per-request
  ``version=`` knob.

We exercise this against a local nginx that listens on
``127.0.0.1:8443`` (h2-capable) and ``127.0.0.1:8444`` (http/1.1-only).
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

import gevent
import pytest

from geventhttpclient.client import HTTPClient

NGINX_HOST = "127.0.0.1"
NGINX_H2_PORT = 8443
NGINX_H1_PORT = 8444
NGINX_PID_FILE = "/tmp/pi/nginx/nginx.pid"
NGINX_CONFIG = "/tmp/pi/nginx/nginx.conf"
NGINX_PREFIX = "/tmp/pi/nginx"
STARTUP_TIMEOUT = 5.0


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _start_nginx() -> None:
    if _port_open(NGINX_HOST, NGINX_H2_PORT) and _port_open(NGINX_HOST, NGINX_H1_PORT):
        return
    if not os.path.exists(NGINX_CONFIG):
        pytest.skip(f"{NGINX_CONFIG} missing")
    try:
        subprocess.Popen(
            ["nginx", "-c", NGINX_CONFIG, "-p", NGINX_PREFIX],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        pytest.skip("nginx binary not available")
    deadline = time.time() + STARTUP_TIMEOUT
    while time.time() < deadline:
        if _port_open(NGINX_HOST, NGINX_H2_PORT) and _port_open(NGINX_HOST, NGINX_H1_PORT):
            return
        gevent.sleep(0.05)
    pytest.skip("nginx did not bind both ports in time")


@pytest.fixture(autouse=True)
def _nginx_session():
    _start_nginx()
    yield


class TestHttpxStyleEnable:
    """Verifies the default-off / opt-in behaviour."""

    def test_default_is_http1(self) -> None:
        c = HTTPClient(
            NGINX_HOST, port=NGINX_H1_PORT,
            ssl=True, insecure=True,
        )
        try:
            # No ``enable_http2=True`` -> the h1 path serves the
            # request, no auto-upgrade.
            assert c._h2_pool is None
            # Confirm we get a real h1 response.
            r = c.request("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H1_PORT}"})
            assert r.status_code == 200
        finally:
            c.close()

    def test_enable_http2_then_h2_succeeds(self) -> None:
        c = HTTPClient(
            NGINX_HOST, port=NGINX_H2_PORT,
            ssl=True, insecure=True, enable_http2=True,
        )
        try:
            assert c._h2_pool is not None
            r = c.request_h2("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H2_PORT}"})
            assert r.status_code == 200
            from geventhttpclient.http2_session import HTTP2ResponseHandle
            assert isinstance(r, HTTP2ResponseHandle)
        finally:
            c.close()

    def test_enable_http2_then_h1_server_falls_back(self) -> None:
        """enable_http2=True + h1-only server -> transparent HTTP/1.1
        fallback. The client returns an ``HTTPSocketPoolResponse``,
        not a stream handle."""
        c = HTTPClient(
            NGINX_HOST, port=NGINX_H1_PORT,
            ssl=True, insecure=True, enable_http2=True,
        )
        try:
            r = c.request_h2("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H1_PORT}"})
            assert r.status_code == 200
            assert hasattr(r, "_sent_request"), (
                f"expected HTTP/1.1 response after fallback, got {type(r).__name__}"
            )
        finally:
            c.close()

    def test_enable_http2_then_unreachable_raises_http2_error(self) -> None:
        from geventhttpclient._http2_errors import HTTP2Error
        c = HTTPClient(
            "127.0.0.1", port=1,  # closed port -> connection refused
            ssl=True, insecure=True, enable_http2=True,
        )
        try:
            with pytest.raises((HTTP2Error, ConnectionError)):
                c.request_h2("GET", "/", headers={"host": "127.0.0.1:1"})
        finally:
            c.close()
