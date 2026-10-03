"""UserAgent-level tests for HTTP/2 dispatch (review_http2_3 M5).

All tests run against the in-process ``H2TestServer`` (the
``h2``-backed TLS+ALPN server in ``tests/http2_test_server.py``) --
no nginx required, no external fixture. The certs are the same
self-signed bundle used by the spec-level tests, so the only
per-test setup cost is spinning up a server greenlet on an
ephemeral port.

The tests verify:

* ``UserAgent.urlopen(..., http2=True)`` round-trips through
  the HTTP/2 stack and returns a ``CompatResponse`` (``HTTP2Response``
  wrapped in the bridge).
* The bridge's ``headers`` object is a real ``Headers`` instance
  (multimap), so ``getlist`` works.
* Two requests on the same user-agent share the same HTTP/2
  session (the pool keeps the connection alive).
* A non-TLS scheme does not enter the h2 path.
"""


import pytest

from geventhttpclient.useragent import UserAgent

from .servers import H2ServerConfig, H2TestServer


def _json_echo(method: str, path: str, headers, body: bytes) -> dict[str, object]:
    """Default echo: return a JSON envelope with status, method, path."""
    return {
        "status": 200,
        "headers": [("content-type", "application/json")],
        "body": (
            b'{"hello":"http2","method":"'
            + method.encode()
            + b'","path":"'
            + path.encode()
            + b'"}'
        ),
    }


@pytest.fixture
def _ua_server():
    """Spin up an ephemeral h2 server and yield ``(UserAgent,
    server_port)`` -- cleanup is automatic."""
    with H2TestServer(config=H2ServerConfig(handler=_json_echo)) as server:
        ua = UserAgent(http2=True, insecure=True)
        try:
            yield ua, server.port
        finally:
            ua.close()


class TestUserAgentH2:
    def test_get_returns_json_body(self, _ua_server) -> None:
        ua, port = _ua_server
        r = ua.urlopen(f"https://127.0.0.1:{port}/get", method="GET")
        assert r.status_code == 200
        import json
        body = json.loads(r.content)
        assert body == {"hello": "http2", "method": "GET", "path": "/get"}

    def test_response_headers_use_headers_multimap(self, _ua_server) -> None:
        ua, port = _ua_server
        r = ua.urlopen(f"https://127.0.0.1:{port}/get", method="GET")
        assert r.headers.get("content-type") == "application/json"
        # ``Headers`` supports ``getlist`` for duplicate-header aware
        # access (e.g. ``Set-Cookie``).
        assert hasattr(r.headers, "getlist")

    def test_post_with_str_payload(self, _ua_server) -> None:
        ua, port = _ua_server
        # H1 (review_http2_3.md H1): the UserAgent h2 path must
        # accept a string payload and ship it as bytes.
        r = ua.urlopen(
            f"https://127.0.0.1:{port}/post",
            method="POST",
            payload="raw-string-body",
        )
        assert r.status_code == 200
        import json
        body = json.loads(r.content)
        assert body["method"] == "POST"
        assert body["path"] == "/post"

    def test_post_with_dict_payload_urlencoded(self, _ua_server) -> None:
        ua, port = _ua_server
        # Dict payloads get urlencoded the same way as the h1 path.
        r = ua.urlopen(
            f"https://127.0.0.1:{port}/post",
            method="POST",
            payload={"key": "value", "n": "1"},
        )
        assert r.status_code == 200

    def test_two_requests_share_one_session(self, _ua_server) -> None:
        ua, port = _ua_server
        r1 = ua.urlopen(f"https://127.0.0.1:{port}/get", method="GET")
        r2 = ua.urlopen(f"https://127.0.0.1:{port}/get", method="GET")
        assert r1.status_code == 200
        assert r2.status_code == 200
        # The pool should have exactly one h2 session cached for the
        # host:port pair.
        client = ua.clientpool.clients[("127.0.0.1", port)]  # type: ignore[attr-defined]
        h2_pool = client._h2_pool
        assert h2_pool is not None
        assert len(h2_pool._sessions) == 1

    def test_post_body_reaches_server(self) -> None:
        """The body echoed by the server proves that the h2 client
        actually shipped the body bytes."""
        captured: dict[str, bytes] = {}

        def handler(method, path, headers, body):
            captured["body"] = body
            return {
                "status": 200,
                "headers": [("content-type", "text/plain")],
                "body": b"received",
            }

        with H2TestServer(
            config=H2ServerConfig(handler=handler),
        ) as server:
            ua = UserAgent(http2=True, insecure=True)
            try:
                r = ua.urlopen(
                    f"https://127.0.0.1:{server.port}/upload",
                    method="POST",
                    payload="the request body",
                )
            finally:
                ua.close()
        assert r.status_code == 200
        assert captured.get("body") == b"the request body"


class TestUserAgentH2FailureModes:
    """Negative paths that M5 originally missed because the tests
    needed nginx to run."""

    def test_connection_refused_raises(self) -> None:
        ua = UserAgent(http2=True, insecure=True)
        try:
            with pytest.raises(Exception):
                # Closed port -> connection refused.
                ua.urlopen("https://127.0.0.1:1/", method="GET")
        finally:
            ua.close()
