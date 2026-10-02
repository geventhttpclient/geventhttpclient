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

import pytest

from geventhttpclient.client import HTTPClient

# Reuse the shared nginx lifecycle from the live-suite module: same
# daemon, same skip semantics, and the session finalizer stops nginx
# again when this run was the one that started it.
from tests.test_http2_session_live import (
    NGINX_HOST,
    _start_nginx,
)
from tests.test_http2_session_live import (
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
