"""Real-world HTTP/2 tests against public h2 servers.

Marked ``network``: deselected by default, needs internet access.
Run explicitly with::

    python -m pytest tests/test_http2_network.py -v

These complement the nginx-fixture tests by exercising the client
against production stacks (Google GFE, GitHub, Cloudflare, nghttp2,
httpbin): real ALPN negotiation, real HPACK dictionaries, gzip and
brotli content encoding, redirects, and stream multiplexing on one
connection. Server availability is outside our control, so a test
skips when its host is unreachable rather than failing.
"""

from __future__ import annotations

import socket
import ssl

import gevent.pool
import pytest

from geventhttpclient.client import HTTPClient
from geventhttpclient.http2_session import HTTP2ResponseHandle
from geventhttpclient.useragent import UserAgent


def _h2_available(host: str, port: int = 443) -> bool:
    """True if the host negotiates ``h2`` via ALPN."""
    try:
        ctx = ssl.create_default_context()
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock = socket.create_connection((host, port), timeout=5)
        sock = ctx.wrap_socket(sock, server_hostname=host)
        proto = sock.selected_alpn_protocol()
        sock.close()
        return proto == "h2"
    except OSError:
        return False


def _skip_if_no_h2(host: str) -> None:
    if not _h2_available(host):
        pytest.skip(f"{host} does not negotiate h2 (or unreachable)")


@pytest.mark.network
class TestRealWorldH2Servers:
    """Single GET against production h2 stacks."""

    @pytest.mark.parametrize(
        "host,path",
        [
            ("nghttp2.org", "/"),
            ("nghttp2.org", "/documentation/"),
            ("github.com", "/"),
            ("httpbin.org", "/html"),
        ],
    )
    def test_get_over_h2(self, host: str, path: str) -> None:
        _skip_if_no_h2(host)
        c = HTTPClient(host, port=443, ssl=True, http2=True)
        try:
            h = c.request_h2("GET", path, headers={"host": host})
            assert isinstance(h, HTTP2ResponseHandle)
            assert h.status_code == 200
            assert len(h.body) > 0
        finally:
            c.close()


@pytest.mark.network
class TestMultiplexing:
    """Several files over ONE h2 connection (stream multiplexing)."""

    def test_concurrent_requests_share_one_session(self) -> None:
        # nghttp2.org is the canonical h2 reference server and its
        # MAX_CONCURRENT_STREAMS (100) comfortably fits the pool size
        # below; google.com was dropped as SUT -- GFE serves captcha
        # pages to datacenter IPs, which made the check flaky.
        _skip_if_no_h2("nghttp2.org")
        c = HTTPClient("nghttp2.org", port=443, ssl=True, http2=True)
        try:
            pool = gevent.pool.Pool(20)

            def fetch(i: int) -> tuple[int, int]:
                h = c.request_h2(
                    "GET", f"/?i={i}",
                    headers={"host": "nghttp2.org"},
                )
                return i, h.status_code

            jobs = [pool.spawn(fetch, i) for i in range(20)]
            results = [j.get(timeout=60) for j in jobs]
            assert all(status == 200 for _, status in results)
            # Every request rode the same connection.
            assert c._h2_pool is not None
            assert len(c._h2_pool._sessions) == 1
        finally:
            c.close()


@pytest.mark.network
class TestLargeDownloads:
    def test_large_file_via_h2(self) -> None:
        _skip_if_no_h2("proof.ovh.net")
        c = HTTPClient("proof.ovh.net", port=443, ssl=True, http2=True)
        try:
            h = c.request_h2(
                "GET", "/files/1Mb.dat",
                headers={"host": "proof.ovh.net"},
            )
            assert h.status_code == 200
            assert len(h.body) == 1024 * 1024
        finally:
            c.close()

    def test_streaming_read(self) -> None:
        _skip_if_no_h2("proof.ovh.net")
        from geventhttpclient.http2_response import HTTP2Response
        c = HTTPClient("proof.ovh.net", port=443, ssl=True, http2=True)
        try:
            h = c.request_h2(
                "GET", "/files/1Mb.dat",
                headers={"host": "proof.ovh.net"},
            )
            resp = HTTP2Response(h)
            total = sum(len(chunk) for chunk in resp.iter_content(chunk_size=65536))
            assert total == 1024 * 1024
        finally:
            c.close()


@pytest.mark.network
class TestFallbackOnH1OnlyServer:
    def test_hetzner_speed_falls_back_to_h1(self) -> None:
        """ash-speed.hetzner.com serves HTTP/1.1 only: with
        ``http2=True`` the request transparently rides the h1 pool
        (httpx semantics) instead of failing."""
        host = "ash-speed.hetzner.com"
        if _h2_available(host):
            pytest.skip(f"{host} unexpectedly negotiates h2 now")
        c = HTTPClient(host, port=443, ssl=True, http2=True)
        try:
            r = c.request_h2(
                "GET", "/100MB.bin",
                headers={"host": host, "range": "bytes=0-1023"},
            )
            assert not isinstance(r, HTTP2ResponseHandle)
            assert r.status_code == 206
            assert len(r.read()) == 1024
        finally:
            c.close()


@pytest.mark.network
class TestContentEncoding:
    """Decompression is a UserAgent-layer feature; both transports
    must surface it identically via ``response.content``."""

    @pytest.mark.parametrize("http2", [False, True])
    def test_gzip_content(self, http2: bool) -> None:
        _skip_if_no_h2("httpbin.org")
        import json
        ua = UserAgent(insecure=True, http2=http2)
        try:
            r = ua.urlopen(
                "https://httpbin.org/gzip",
                headers={"accept-encoding": "gzip"},
            )
            assert r.status_code == 200
            assert json.loads(r.content)["gzipped"] is True
        finally:
            ua.close()

    @pytest.mark.parametrize("http2", [False, True])
    def test_brotli_content(self, http2: bool) -> None:
        _skip_if_no_h2("httpbin.org")
        import json
        ua = UserAgent(insecure=True, http2=http2)
        try:
            r = ua.urlopen(
                "https://httpbin.org/brotli",
                headers={"accept-encoding": "br"},
            )
            assert r.status_code == 200
            assert json.loads(r.content)["brotli"] is True
        finally:
            ua.close()
