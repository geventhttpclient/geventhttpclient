"""Tests for :mod:`geventhttpclient.http2_response`.

Covers the :class:`HTTP2Response` read/iter API and the retry logic in
:meth:`HTTPClient.request_h2` (Sprint 3c).
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from geventhttpclient.http2_response import HTTP2Response, HTTP2ResponseError

# Reuse the FakeSocket helpers from test_http2_session
sys.path.insert(0, "tests")
from test_http2_session import (
    FakeSocket,
    _server_drive,
    _server_replies_with,
)

from geventhttpclient._http2_parser import session_server_new
from geventhttpclient.http2_pool import HTTP2ConnectionPool, HTTP2ConnectionPoolError
from geventhttpclient.http2_session import (
    HTTP2ResponseHandle,
    HTTP2Session,
    HTTP2WireError,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_round_trip(
    response_body: bytes = b"hello world",
    *,
    status: int = 200,
    content_type: str = "text/plain",
    chunks: list[bytes] | None = None,
) -> HTTP2ResponseHandle:
    """Wire a one-off HTTP2Session against a fake server, submit a GET,
    and return the closed response handle."""
    fs = FakeSocket()
    client_sock = fs.side_a()
    server_sock = fs.side_b()

    session = HTTP2Session(client_sock)
    server_session = session_server_new()
    server_events: list[Any] = []

    def on_request(stream_id: int, headers_list: list[Any]) -> None:
        if chunks is not None:
            response = server_session.submit_response(
                stream_id,
                [(":status", str(status)), ("content-type", content_type)],
                with_body=True,
            )
            for chunk in chunks[:-1]:
                response += server_session.submit_data(
                    stream_id, chunk, end_stream=False
                )
            response += server_session.submit_data(
                stream_id, chunks[-1], end_stream=True
            )
            server_sock.sendall(response)
        else:
            _server_replies_with(
                server_sock, server_session, stream_id, status, response_body,
                content_type=content_type,
            )

    _server_drive(server_sock, server_session, server_events, on_request)

    handle = session.submit_request("GET", "/", "example.com")
    for _ in range(50):
        if handle.is_closed:
            break
        try:
            session.drive_once()
        except HTTP2WireError:
            pass
        _server_drive(server_sock, server_session, server_events, on_request)

    assert handle.is_closed, f"handle never closed; events={server_events[-2:]}"
    return handle


# ---------------------------------------------------------------------------
# HTTP2Response API tests
# ---------------------------------------------------------------------------


class TestRead:
    def test_read_all(self) -> None:
        h = _make_round_trip(b"the body")
        r = HTTP2Response(h)
        assert r.read() == b"the body"
        assert r.status_code == 200

    def test_read_n_bytes(self) -> None:
        h = _make_round_trip(b"abcdefghij")
        r = HTTP2Response(h)
        assert r.read(3) == b"abc"
        assert r.read(3) == b"def"
        assert r.read(10) == b"ghij"

    def test_read_returns_empty_at_eof(self) -> None:
        h = _make_round_trip(b"")
        r = HTTP2Response(h)
        assert r.read() == b""
        assert r.read(1024) == b""


class TestIterContent:
    def test_iter_chunks(self) -> None:
        h = _make_round_trip(b"x" * 100, chunks=[b"x" * 100])
        r = HTTP2Response(h)
        chunks = list(r.iter_content(chunk_size=10))
        assert b"".join(chunks) == b"x" * 100
        # All chunks except possibly the last are exactly chunk_size.
        for c in chunks[:-1]:
            assert len(c) == 10

    def test_iter_handles_multi_frame_body(self) -> None:
        h = _make_round_trip(
            chunks=[b"chunk-1-", b"chunk-2-", b"chunk-3"],
        )
        r = HTTP2Response(h)
        assert b"".join(r.iter_content()) == b"chunk-1-chunk-2-chunk-3"


class TestIterLines:
    def test_basic_lines(self) -> None:
        h = _make_round_trip(b"alpha\r\nbeta\r\ngamma")
        r = HTTP2Response(h)
        lines = list(r.iter_lines())
        assert lines == [b"alpha", b"beta", b"gamma"]


class TestJson:
    def test_json_body(self) -> None:
        h = _make_round_trip(b'{"key":"value","n":1}')
        r = HTTP2Response(h)
        assert r.json() == {"key": "value", "n": 1}


class TestRaiseForStatus:
    @pytest.mark.parametrize("status", [400, 404, 500, 503])
    def test_4xx_5xx_raises(self, status: int) -> None:
        h = _make_round_trip(b"", status=status)
        r = HTTP2Response(h)
        with pytest.raises(HTTP2ResponseError, match=str(status)):
            r.raise_for_status()

    @pytest.mark.parametrize("status", [200, 201, 204, 301, 302, 304])
    def test_2xx_3xx_does_not_raise(self, status: int) -> None:
        h = _make_round_trip(b"", status=status)
        r = HTTP2Response(h)
        r.raise_for_status()  # must not raise


class TestContextManager:
    def test_with_statement(self) -> None:
        h = _make_round_trip(b"hi")
        with HTTP2Response(h) as r:
            assert r.read() == b"hi"


# ---------------------------------------------------------------------------
# Retry tests (via fake-socket pool)
# ---------------------------------------------------------------------------


class _BrokenFirstOpenPool(HTTP2ConnectionPool):
    """HTTP2ConnectionPool subclass whose first ``_open_socket`` raises.

    Used to simulate a transient connection error on the first attempt.
    """

    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.open_calls = 0

    def _open_socket(self, host: str, port: int) -> Any:
        self.open_calls += 1
        if self.open_calls == 1:
            raise HTTP2ConnectionPoolError("simulated transient failure")
        return super()._open_socket(host, port)


class TestRetry:
    """`request_h2` retries idempotent methods on transient errors."""

    def test_no_retry_without_max_retries(self) -> None:
        pool = _BrokenFirstOpenPool(insecure=True)
        try:
            client = _make_http_client(pool, host="never-resolves")
            with pytest.raises(RuntimeError, match="HTTP/2 connection failed"):
                client.request_h2(
                    "GET", "/", max_retries=0,
                )
        finally:
            pool.close()

    def test_get_retries_then_succeeds(self) -> None:
        # The retry path needs the second attempt to actually succeed
        # — we monkey-patch _open_socket to raise on the first call
        # and succeed on the second. We rely on the live nginx server
        # (skipped when nginx is not running).
        from test_http2_session_live import NGINX_HOST, NGINX_PORT, _start_nginx
        _start_nginx()

        from geventhttpclient import client as client_module

        real_open = client_module.HTTP2ConnectionPool._open_socket
        calls = {"n": 0}

        def flaky_open(self, host, port):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] == 1:
                raise HTTP2ConnectionPoolError("transient")
            return real_open(self, host, port)

        client_module.HTTP2ConnectionPool._open_socket = flaky_open  # type: ignore[method-assign]
        try:
            client = _make_http_client(
                client_module.HTTP2ConnectionPool(insecure=True),
                host=NGINX_HOST,
                port=NGINX_PORT,
            )
            handle = client.request_h2(
                "GET", "/get", max_retries=1, timeout=5.0,
            )
            assert handle.status_code == 200
            assert calls["n"] == 2
        finally:
            client_module.HTTP2ConnectionPool._open_socket = real_open  # type: ignore[method-assign]

    def test_post_does_not_retry_on_send_error(self) -> None:
        # Even with max_retries > 0, POST must not auto-retry on a
        # connection error (RFC 9110 §9.2.2).
        from geventhttpclient import client as client_module

        real_open = client_module.HTTP2ConnectionPool._open_socket
        calls = {"n": 0}

        def always_fail(self, host, port):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise HTTP2ConnectionPoolError("never resolves")

        client_module.HTTP2ConnectionPool._open_socket = always_fail  # type: ignore[method-assign]
        try:
            client = _make_http_client(
                client_module.HTTP2ConnectionPool(insecure=True),
            )
            with pytest.raises(RuntimeError):
                client.request_h2("POST", "/", body=b"x", max_retries=3)
            # POST should NOT retry, so we must have made a single
            # attempt (plus the one we count in the wrapper itself).
            assert calls["n"] == 1
        finally:
            client_module.HTTP2ConnectionPool._open_socket = real_open  # type: ignore[method-assign]


def _make_http_client(
    pool: HTTP2ConnectionPool,
    host: str = "127.0.0.1",
    port: int = 443,
) -> Any:
    """Construct an HTTPClient wired with the supplied h2 pool.

    Monkey-patches the HTTPClient constructor to skip pool creation (we
    pass our own). For tests that don't go through real network, the
    connection details are irrelevant.
    """
    from geventhttpclient.client import HTTPClient
    from geventhttpclient.header import Headers

    client = HTTPClient.__new__(HTTPClient)
    # Replicate the parts of __init__ we need for request_h2.
    client.host = host
    client.port = port
    client.ssl = True
    client._h2_pool = pool
    client.enable_http2 = True
    client.headers_type = Headers
    client.default_headers = Headers()
    client.DEFAULT_HEADERS = Headers()
    return client
