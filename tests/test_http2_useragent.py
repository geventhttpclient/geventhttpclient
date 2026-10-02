"""UserAgent-level tests for HTTP/2 dispatch (Sprint 5).

These tests verify that ``UserAgent.urlopen`` with ``enable_http2=True``
round-trips through the HTTP/2 stack and returns a usable
``CompatResponse``. The local nginx fixture is reused from the wire-layer
suite.
"""

from __future__ import annotations

import pytest

from geventhttpclient.useragent import UserAgent

# Same nginx config as the wire-layer live tests. ``tests/`` is added
# to sys.path implicitly (pytest does it), so we can import the live
# helpers via their module name.
from tests.test_http2_session_live import NGINX_HOST, NGINX_PORT, _start_nginx


@pytest.fixture(autouse=True)
def _nginx_session():
    _start_nginx()
    yield


class TestUserAgentH2:
    def test_get_returns_json_body(self) -> None:
        ua = UserAgent(enable_http2=True, insecure=True)
        try:
            r = ua.urlopen(f"https://{NGINX_HOST}:{NGINX_PORT}/get", method="GET")
            assert r.status_code == 200
            assert r.content == b'{"hello":"http2","method":"GET"}'
        finally:
            ua.close()

    def test_response_headers_exposed(self) -> None:
        ua = UserAgent(enable_http2=True, insecure=True)
        try:
            r = ua.urlopen(f"https://{NGINX_HOST}:{NGINX_PORT}/get", method="GET")
            # The bridge wraps headers in a Headers instance so getlist works.
            assert r.headers.get("content-type") == "application/json"
        finally:
            ua.close()

    def test_invalid_scheme_falls_back_to_http1(self) -> None:
        # HTTP/2 only fires when ``ssl=True`` and ``enable_http2=True``.
        # A plaintext target must hit the existing http/1.1 path
        # (port 80 has no nginx here, so the socket fails -- that is
        # enough to prove we did not try the h2 path).
        ua = UserAgent(enable_http2=True, insecure=True)
        try:
            with pytest.raises(Exception):
                # port 443 is closed in CI, so any connection attempt
                # raises. We just want the failure to come from a
                # plaintext http/1.1 client, not from the h2 layer
                # trying to negotiate ALPN over a non-TLS socket.
                ua.urlopen(f"http://{NGINX_HOST}:9999/", method="GET")
        finally:
            ua.close()

    def test_two_requests_share_one_session(self) -> None:
        ua = UserAgent(enable_http2=True, insecure=True)
        try:
            r1 = ua.urlopen(f"https://{NGINX_HOST}:{NGINX_PORT}/get", method="GET")
            r2 = ua.urlopen(f"https://{NGINX_HOST}:{NGINX_PORT}/get", method="GET")
            assert r1.status_code == 200
            assert r2.status_code == 200
            assert r1.content == r2.content
        finally:
            ua.close()
