"""Phase 6 tests: ALPN setup + version="auto" fallback to HTTP/1.1.

The ``HTTPClient`` learns the peer's choice via ``selected_alpn_protocol``
on the underlying socket. When the peer did not negotiate ``h2`` we
fall back to the HTTP/1.1 pool instead of raising. The
``SSLConnectionPool`` advertises ``["h2", "http/1.1"]`` by default so
h2-capable servers prefer the h2 ALPN.

A local nginx serves both ``8443`` (h2) and ``8444`` (http/1.1
fallback) so we can exercise both code paths.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

import gevent
import gevent.ssl
import pytest

from geventhttpclient.client import HTTPClient
from geventhttpclient.connectionpool import SSLConnectionPool

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


class TestSSLConnectionPoolALPN:
    def test_default_advertises_h2(self) -> None:
        """Setting up an ``SSLConnectionPool`` with the default ALPN
        list must not raise and must install the context on the pool."""
        pool = SSLConnectionPool(
            connection_host=NGINX_HOST, connection_port=NGINX_H2_PORT,
            request_host=NGINX_HOST, request_port=NGINX_H2_PORT,
            insecure=True,
        )
        # ``set_alpn_protocols`` was called without raising -- that is
        # what we need. The stdlib SSLContext does not expose the
        # advertised list (it is private state), so we verify the
        # negotiated outcome via the live round-trip below.
        assert pool.ssl_context is not None

    def test_empty_alpn_protocols_disables_alpn(self) -> None:
        """An empty ALPN list is accepted (no ALPN negotiation)."""
        pool = SSLConnectionPool(
            connection_host=NGINX_HOST, connection_port=NGINX_H2_PORT,
            request_host=NGINX_HOST, request_port=NGINX_H2_PORT,
            insecure=True,
            alpn_protocols=[],
        )
        assert pool.ssl_context is not None


class TestHTTPClientVersionDispatch:
    def test_version_auto_falls_back_to_http11(self) -> None:
        """``version="auto"`` on a server that does not negotiate ``h2``
        must fall back to the HTTP/1.1 pool transparently."""
        c = HTTPClient(
            NGINX_HOST,
            port=NGINX_H1_PORT,
            ssl=True,
            insecure=True,
            enable_http2=True,
        )
        try:
            result = c.request_h2(
                "GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H1_PORT}"},
                version="auto",
            )
            # Fallback returned an HTTP/1.1 response object, not the
            # h2 handle.
            assert hasattr(result, "_sent_request"), (
                f"expected HTTPSocketPoolResponse, got {type(result).__name__}"
            )
        finally:
            c.close()

    def test_version_2_raises_on_no_h2(self) -> None:
        """``version="2"`` forces HTTP/2 and refuses to fall back."""
        c = HTTPClient(
            NGINX_HOST,
            port=NGINX_H1_PORT,
            ssl=True,
            insecure=True,
            enable_http2=True,
        )
        try:
            with pytest.raises(RuntimeError, match="HTTP/2"):
                c.request_h2(
                    "GET", "/get",
                    headers={"host": f"{NGINX_HOST}:{NGINX_H1_PORT}"},
                    version="2",
                )
        finally:
            c.close()

    def test_version_auto_on_h2_returns_handle(self) -> None:
        """``version="auto"`` on an h2-capable server returns the
        HTTP2ResponseHandle."""
        c = HTTPClient(
            NGINX_HOST,
            port=NGINX_H2_PORT,
            ssl=True,
            insecure=True,
            enable_http2=True,
        )
        try:
            result = c.request_h2(
                "GET", "/get",
                headers={"host": f"{NGINX_HOST}:{NGINX_H2_PORT}"},
                version="auto",
            )
            from geventhttpclient.http2_session import HTTP2ResponseHandle
            assert isinstance(result, HTTP2ResponseHandle), (
                f"expected HTTP2ResponseHandle, got {type(result).__name__}"
            )
        finally:
            c.close()
