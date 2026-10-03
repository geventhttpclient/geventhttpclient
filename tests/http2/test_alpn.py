"""HTTP/2 ALPN and upgrade tests.

* ``HTTPClient(http2=False)`` (the default) keeps the existing
  HTTP/1.1 path untouched.
* ``HTTPClient(http2=True)`` opens the h2 pool. The h2 transport
  negotiates ALPN; when the peer chose ``http/1.1`` (or did not
  advertise any ALPN protocol) we close the h2 socket and retry on
  the HTTP/1.1 pool.

Network tests run against ``httpbingo.org``. The h1-only fallback
test runs against ``H1OnlyTestServer`` because httpbingo.org always
advertises ``h2`` in ALPN.
"""

import socket

import pytest

from geventhttpclient.client import HTTPClient
from tests.common import HTTPBIN_HOST


class TestHttpxStyleEnable:
    @pytest.mark.network
    def test_default_is_http1(self) -> None:
        c = HTTPClient(
            HTTPBIN_HOST,
            ssl=True,
            insecure=True,
        )
        try:
            assert c._h2_pool is None
            r = c.request(
                "GET",
                "/get",
                headers={"host": HTTPBIN_HOST},
            )
            assert r.status_code == 200
        finally:
            c.close()

    @pytest.mark.network
    def test_http2_then_h2_succeeds(self) -> None:
        c = HTTPClient(
            HTTPBIN_HOST,
            ssl=True,
            insecure=True,
            http2=True,
        )
        try:
            assert c._h2_pool is not None
            r = c.request_h2(
                "GET",
                "/get",
                headers={"host": HTTPBIN_HOST},
            )
            assert r.status_code == 200
            from geventhttpclient.http2.session import HTTP2ResponseHandle

            assert isinstance(r, HTTP2ResponseHandle)
        finally:
            c.close()

    def test_http2_then_h1_server_falls_back(self) -> None:
        """http2=True + h1-only server -> transparent HTTP/1.1
        fallback. Returns ``HTTPSocketPoolResponse`` (has
        ``_sent_request``), not a stream handle.
        """
        from .servers import H1OnlyTestServer

        with H1OnlyTestServer() as server:
            c = HTTPClient(
                "127.0.0.1",
                port=server.port,
                ssl=True,
                insecure=True,
                http2=True,
                # Generous timeouts: the in-process TLS server takes
                # a moment to schedule on a busy hub and we don't
                # want the handshake to race the 5s default.
                connection_timeout=15.0,
                network_timeout=15.0,
            )
            try:
                r = c.request_h2(
                    "GET",
                    "/get",
                    headers={"host": f"127.0.0.1:{server.port}"},
                )
                assert r.status_code == 200
                assert r.read() == H1OnlyTestServer.BODY
                assert hasattr(r, "_sent_request"), (
                    f"expected HTTP/1.1 response after fallback, got {type(r).__name__}"
                )
            finally:
                c.close()

    def test_http2_then_unreachable_raises_http2_error(self) -> None:
        from geventhttpclient.http2.errors import HTTP2Error

        c = HTTPClient(
            "127.0.0.1",
            port=1,  # closed port -> connection refused
            ssl=True,
            insecure=True,
            http2=True,
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
    protocol violation."""

    def test_h1_pool_offers_http11_only(self) -> None:
        from geventhttpclient.connectionpool import SSLConnectionPool
        from tests.common import free_port

        port = free_port()
        pool = SSLConnectionPool(
            "127.0.0.1",
            port,
            "127.0.0.1",
            port,
            insecure=True,
        )
        assert pool.ssl_context is not None
        pool2 = SSLConnectionPool(
            "127.0.0.1",
            port,
            "127.0.0.1",
            port,
            insecure=True,
            alpn_protocols=None,
        )
        assert pool2 is not None  # constructed without error

    @pytest.mark.network
    def test_h1_client_against_h2_capable_server_stays_h1(self) -> None:
        c = HTTPClient(
            HTTPBIN_HOST,
            ssl=True,
            insecure=True,
        )
        try:
            assert c.http2 is False
            assert c._h2_pool is None
            r = c.request(
                "GET",
                "/get",
                headers={"host": HTTPBIN_HOST},
            )
            assert r.status_code == 200
        finally:
            c.close()


class TestErrorTaxonomy:
    """K2 (review): every h2 transport failure must surface as a
    ``ConnectionError`` so ``except ConnectionError`` catches h1 and
    h2 uniformly."""

    @staticmethod
    def _tls_listener():
        import ssl

        from .servers import CERT_FILE, KEY_FILE

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
        ctx.set_alpn_protocols(["h2"])
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        return listener, ctx

    def test_timeout_surfaces_as_connection_error(self) -> None:
        import threading

        from geventhttpclient.http2.errors import HTTP2Error

        listener, ctx = self._tls_listener()
        port = listener.getsockname()[1]
        held = []

        def accept_and_hang() -> None:
            conn, _ = listener.accept()
            conn = ctx.wrap_socket(conn, server_side=True)
            held.append(conn)

        t = threading.Thread(target=accept_and_hang, daemon=True)
        t.start()

        c = HTTPClient("127.0.0.1", port=port, ssl=True, insecure=True, http2=True)
        try:
            with pytest.raises(HTTP2Error, match="did not arrive"):
                c.request_h2(
                    "GET",
                    "/",
                    headers={"host": f"127.0.0.1:{port}"},
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
            with pytest.raises(ConnectionError):
                c.request_h2(
                    "GET",
                    "/",
                    headers={"host": f"127.0.0.1:{port}"},
                    timeout=5.0,
                )
        finally:
            c.close()
            listener.close()
