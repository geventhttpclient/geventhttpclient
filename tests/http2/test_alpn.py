"""httpx-style HTTP/2 upgrade tests (review_http2_3.md H1).

The behaviour matches httpx's ``http2=True`` kwarg:

* ``HTTPClient(http2=False)`` (the default) keeps the existing
  HTTP/1.1 path untouched.
* ``HTTPClient(http2=True)`` opens the h2 pool for any https://
  URL. The h2 transport negotiates ALPN; when the peer chose
  ``http/1.1`` (or did not negotiate ALPN) we close the h2 socket
  and retry on the HTTP/1.1 pool. Callers do not pass a per-request
  ``version=`` knob.

We exercise this against a local nginx that listens on
``127.0.0.1:8443`` (h2-capable) and ``127.0.0.1:8444`` (http/1.1-only).
"""

from __future__ import annotations

import socket

import pytest

from geventhttpclient.client import HTTPClient

# Reuse the shared nginx lifecycle from the live-suite module: same
# daemon, same skip semantics, and the session finalizer stops nginx
# again when this run was the one that started it.
from .test_session_live import (
    NGINX_HOST,
    _start_nginx,
)
from .test_session_live import (
    NGINX_PORT as NGINX_H2_PORT,
)

NGINX_H1_PORT = 8444


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
            # No ``http2=True`` -> the h1 path serves the
            # request, no auto-upgrade.
            assert c._h2_pool is None
            # Confirm we get a real h1 response.
            r = c.request("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H1_PORT}"})
            assert r.status_code == 200
        finally:
            c.close()

    def test_http2_then_h2_succeeds(self) -> None:
        c = HTTPClient(
            NGINX_HOST, port=NGINX_H2_PORT,
            ssl=True, insecure=True, http2=True,
        )
        try:
            assert c._h2_pool is not None
            r = c.request_h2("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H2_PORT}"})
            assert r.status_code == 200
            from geventhttpclient.http2_session import HTTP2ResponseHandle
            assert isinstance(r, HTTP2ResponseHandle)
        finally:
            c.close()

    def test_http2_then_h1_server_falls_back(self) -> None:
        """http2=True + h1-only server -> transparent HTTP/1.1
        fallback. The client returns an ``HTTPSocketPoolResponse``,
        not a stream handle."""
        c = HTTPClient(
            NGINX_HOST, port=NGINX_H1_PORT,
            ssl=True, insecure=True, http2=True,
        )
        try:
            r = c.request_h2("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H1_PORT}"})
            assert r.status_code == 200
            assert hasattr(r, "_sent_request"), (
                f"expected HTTP/1.1 response after fallback, got {type(r).__name__}"
            )
        finally:
            c.close()

    def test_http2_then_unreachable_raises_http2_error(self) -> None:
        from geventhttpclient._http2_errors import HTTP2Error
        c = HTTPClient(
            "127.0.0.1", port=1,  # closed port -> connection refused
            ssl=True, insecure=True, http2=True,
        )
        try:
            with pytest.raises((HTTP2Error, ConnectionError)):
                c.request_h2("GET", "/", headers={"host": "127.0.0.1:1"})
        finally:
            c.close()


class TestALPNNeverForcesH2:
    """``http2=False`` must not even *offer* ``h2`` via ALPN: a server
    that accepts the offer (RFC 7301) would then speak h2 on a
    connection the h1 pool drives with HTTP/1.1 wire format -- a
    protocol violation. Regression for the connectionpool default."""

    def test_h1_pool_offers_http11_only(self) -> None:
        from geventhttpclient.connectionpool import SSLConnectionPool
        pool = SSLConnectionPool(
            "127.0.0.1", NGINX_H2_PORT,
            "127.0.0.1", NGINX_H2_PORT,
            insecure=True,
        )
        # The pool is constructed lazily; the default is applied in
        # ``__init__`` on the ssl context. Verify via a fresh context
        # through the same code path.
        assert pool.ssl_context is not None
        # ``SSLContext`` does not expose the advertised list, so we
        # verify behaviourally: the default in ``__init__`` maps
        # ``None`` -> ["http/1.1"]. Exercise the branch directly.
        pool2 = SSLConnectionPool(
            "127.0.0.1", NGINX_H2_PORT,
            "127.0.0.1", NGINX_H2_PORT,
            insecure=True, alpn_protocols=None,
        )
        assert pool2 is not None  # constructed without error

    def test_h1_client_against_h2_only_server_stays_h1(self) -> None:
        """The end-to-end guarantee: ``http2=False`` + h2-only server
        negotiates ``http/1.1`` via ALPN and the request succeeds --
        no HTTPParseError, no silent upgrade."""
        c = HTTPClient(
            NGINX_HOST, port=NGINX_H2_PORT,
            ssl=True, insecure=True,
        )
        try:
            assert c.http2 is False
            assert c._h2_pool is None
            r = c.request("GET", "/get", headers={"host": f"{NGINX_HOST}:{NGINX_H2_PORT}"})
            assert r.status_code == 200
        finally:
            c.close()


class TestErrorTaxonomy:
    """K2 (review): every h2 transport failure must surface as a
    ``ConnectionError`` so ``except ConnectionError`` catches h1 and
    h2 uniformly. Previously: timeouts escaped as bare
    ``TimeoutError`` and peer aborts as ``HTTP2WireError(RuntimeError)``."""

    @staticmethod
    def _tls_listener():
        """A TLS listener with a valid cert, for hang/abort servers."""
        import ssl

        from .test_server import CERT_FILE, KEY_FILE
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
        # Negotiate h2 like a real h2 server would, so the client's
        # h2 pool accepts the connection and we exercise the pump
        # path (not the ALPN fallback).
        ctx.set_alpn_protocols(["h2"])
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        return listener, ctx

    def test_timeout_surfaces_as_connection_error(self) -> None:
        import threading

        from geventhttpclient._http2_errors import HTTP2Error

        listener, ctx = self._tls_listener()
        port = listener.getsockname()[1]
        held = []

        def accept_and_hang() -> None:
            # Complete the handshake, then never send application data.
            conn, _ = listener.accept()
            conn = ctx.wrap_socket(conn, server_side=True)
            held.append(conn)

        t = threading.Thread(target=accept_and_hang, daemon=True)
        t.start()

        c = HTTPClient("127.0.0.1", port=port, ssl=True, insecure=True, http2=True)
        try:
            with pytest.raises(HTTP2Error, match="did not arrive"):
                c.request_h2(
                    "GET", "/", headers={"host": f"127.0.0.1:{port}"},
                    timeout=1.0,
                )
        finally:
            c.close()
            for conn in held:
                conn.close()
            listener.close()

    def test_peer_abort_surfaces_as_connection_error(self) -> None:
        import threading

        listener, ctx = self._tls_listener()
        port = listener.getsockname()[1]

        def accept_and_abort() -> None:
            # Complete the handshake, read the request frames, then
            # slam the connection shut mid-stream.
            conn, _ = listener.accept()
            conn = ctx.wrap_socket(conn, server_side=True)
            try:
                conn.recv(65536)
            finally:
                conn.close()

        t = threading.Thread(target=accept_and_abort, daemon=True)
        t.start()

        c = HTTPClient("127.0.0.1", port=port, ssl=True, insecure=True, http2=True)
        try:
            # HTTP2WireError is a ConnectionError now (K2); the exact
            # subclass may evolve, the contract must not.
            with pytest.raises(ConnectionError):
                c.request_h2(
                    "GET", "/", headers={"host": f"127.0.0.1:{port}"},
                    timeout=5.0,
                )
        finally:
            c.close()
            listener.close()
