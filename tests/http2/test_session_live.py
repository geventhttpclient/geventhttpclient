"""Live HTTP/2 round-trips against ``httpbingo.org``.

Tagged ``@pytest.mark.network`` so the pytest-rerunfailures fixture
in ``conftest.py`` retries transient blips. Endpoints used:

* ``GET  /get``         -- 200, JSON body
* ``POST /post``        -- 200, JSON body
* ``GET  /status/404``  -- 404
"""

import pytest

from geventhttpclient.http2.session import HTTP2ResponseHandle, HTTP2Session
from tests.common import HTTPBIN_HOST

DRIVE_TIMEOUT = 15.0


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
        session.drive_once()
        import gevent
        gevent.sleep(0)


def _connect_h2(host: str = HTTPBIN_HOST, port: int = 443) -> object:
    """Open a TLS+ALPN-negotiated connection. Skips if the peer did
    not pick ``h2``."""
    import gevent.socket
    import gevent.ssl

    ctx = gevent.ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = gevent.ssl.CERT_NONE
    ctx.set_alpn_protocols(["h2", "http/1.1"])

    sock = gevent.socket.create_connection((host, port), timeout=DRIVE_TIMEOUT)
    sock = ctx.wrap_socket(sock, server_hostname=host)
    selected = sock.selected_alpn_protocol()
    if selected != "h2":
        sock.close()
        pytest.skip(f"server at {host}:{port} did not negotiate h2 (got {selected!r})")
    return sock


def _default_headers() -> list[tuple[str, str]]:
    """Match what HTTPClient sends in production (User-Agent). Without
    a User-Agent httpbingo.org's Fly.io middleware answers 402
    Payment Required for what it treats as a bot."""
    return [("user-agent", "curl/8.5.0")]


class TestLiveRoundTrip:
    @pytest.mark.network
    def test_get_returns_json_body(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request(
                "GET", "/get", HTTPBIN_HOST, headers=_default_headers(),
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 200
            assert b'"url"' in handle.body
        finally:
            sock.close()

    @pytest.mark.network
    def test_post_request(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request(
                "POST", "/post", HTTPBIN_HOST, headers=_default_headers(),
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 200
            assert b'"form"' in handle.body
            assert b'"data"' in handle.body
        finally:
            sock.close()

    @pytest.mark.network
    def test_concurrent_streams_over_one_connection(self) -> None:
        """Two requests share one h2 session -- verifies the
        sans-ink-out multiplexer against a real server."""
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            hdrs = _default_headers()
            h1 = session.submit_request("GET", "/get", HTTPBIN_HOST, headers=hdrs)
            h2 = session.submit_request("GET", "/get", HTTPBIN_HOST, headers=hdrs)
            # Different stream ids because HTTP/2 increments client streams by 2.
            assert h1.stream_id != h2.stream_id
            _drive_until_closed(session, h1)
            _drive_until_closed(session, h2)
            assert h1.status_code == 200
            assert h2.status_code == 200
            # Bodies are NOT byte-identical because httpbingo echoes
            # the request's ``X-Request-Start`` timestamp which differs
            # per request. We assert on shape instead.
            import json
            for body, label in ((h1.body, "h1"), (h2.body, "h2")):
                parsed = json.loads(body)
                assert parsed["method"] == "GET"
                assert parsed["url"].endswith("/get")
                assert "origin" in parsed
        finally:
            sock.close()

    @pytest.mark.network
    def test_404_path_returns_404(self) -> None:
        sock = _connect_h2()
        try:
            session = HTTP2Session(sock)
            handle = session.submit_request(
                "GET", "/status/404", HTTPBIN_HOST, headers=_default_headers(),
            )
            _drive_until_closed(session, handle)
            assert handle.status_code == 404
        finally:
            sock.close()
