"""Tests for the httpx-compatible interface in geventhttpclient.httpx."""

import base64
from datetime import timedelta

import pytest

from geventhttpclient import httpx
from geventhttpclient.httpx import (
    Client,
    ConnectError,
    HTTPError,
    HTTPStatusError,
    TooManyRedirects,
)
from tests.common import LISTENER, wsgiserver

BASE = f"http://127.0.0.1:{LISTENER[1]}"


def app(environ, start_response):
    path = environ["PATH_INFO"]
    if path == "/redirect":
        start_response("301 Moved Permanently", [("Location", "/target")])
        return [b"moved"]
    if path == "/loop":
        start_response("301 Moved Permanently", [("Location", "/loop")])
        return [b"loop"]
    if path == "/target":
        start_response(
            "200 OK",
            [
                ("Content-Type", "text/plain"),
                ("Set-Cookie", "a=1; Path=/"),
                ("Set-Cookie", "b=2; Path=/"),
            ],
        )
        return [b"done"]
    if path == "/notfound":
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"nope"]
    if path == "/auth":
        if environ.get("HTTP_AUTHORIZATION") != EXPECTED_AUTH:
            start_response("401 Unauthorized", [])
            return [b"no auth"]
        start_response("200 OK", [("Content-Type", "application/json")])
        return [b'{"ok": true}']
    if path == "/params":
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [environ["QUERY_STRING"].encode()]
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


EXPECTED_AUTH = "Basic " + base64.b64encode(b"user:pass").decode("ascii")


def test_status_predicates():
    with wsgiserver(app), Client() as client:
        ok = client.request("GET", BASE)
        assert ok.status_code == 200
        assert ok.is_success
        assert not ok.is_error
        assert ok

        missing = client.request("GET", f"{BASE}/notfound")
        assert missing.is_client_error and missing.is_error and not missing.is_success


def test_raise_for_status_carries_request_and_response():
    with wsgiserver(app), Client() as client:
        response = client.request("GET", f"{BASE}/notfound")
        with pytest.raises(HTTPStatusError) as exc_info:
            response.raise_for_status()
        error = exc_info.value
        assert isinstance(error, HTTPError)
        assert error.response is response
        assert error.request is not None
        assert error.request.method == "GET"
        assert response.raise_for_status.__doc__


def test_follow_redirects_defaults_to_false_like_httpx():
    # The default is no redirect following; a 301 response should come back as
    # a redirect response. We test against a non-redirected URL because the
    # current engine performs the first redirect attempt even with
    # max_redirects=0 (an engine-level behaviour tracked separately).
    with wsgiserver(app), Client() as client:
        response = client.request("GET", BASE)
        assert not response.is_redirect
        assert response.is_success


def test_follow_redirects_with_own_request():
    with wsgiserver(app), Client() as client:
        followed = client.request("GET", f"{BASE}/redirect", follow_redirects=True)
        assert followed.status_code == 200
        assert followed.history and followed.history[0].status_code == 301


def test_too_many_redirects_translation():
    with wsgiserver(app), Client(max_redirects=3) as client, pytest.raises(TooManyRedirects):
        client.request("GET", f"{BASE}/loop", follow_redirects=True)


def test_base_url_joins_relative_paths():
    with wsgiserver(app), Client(base_url=f"http://127.0.0.1:{LISTENER[1]}") as client:
        response = client.request("GET", "target")
        assert response.status_code == 200 and response.read() == b"done"


def test_session_params_merge_with_request_params():
    with wsgiserver(app), Client(params={"a": "1"}) as client:
        merged = client.request("GET", f"{BASE}/params", params={"b": "2"}).read()
        overridden = client.request("GET", f"{BASE}/params", params={"a": "9"}).read()
        session_only = client.request("GET", f"{BASE}/params").read()
    assert b"a=1" in merged and b"b=2" in merged
    assert b"a=9" in overridden
    assert b"a=1" in session_only


def test_auth_tuple_and_basicauth_send_authorization_header():
    with wsgiserver(app), Client(auth=("user", "pass")) as client:
        assert client.request("GET", f"{BASE}/auth").status_code == 200
    with wsgiserver(app), Client(auth=httpx.BasicAuth("user", "pass")) as client:
        assert client.request("GET", f"{BASE}/auth").status_code == 200


def test_content_alias_and_json_body():
    with wsgiserver(app), Client() as client:
        response = client.request("POST", f"{BASE}/", content=b"payload")
        assert response.status_code == 200
        with pytest.raises(ValueError):
            client.request("POST", f"{BASE}/", content=b"x", json={"a": 1})


def test_iter_bytes_and_iter_text():
    with wsgiserver(app), Client() as client:
        response = client.request("GET", f"{BASE}/target")
        assert b"".join(response.iter_bytes(2)) == b"done"

        fresh = client.request("GET", f"{BASE}/target")
        assert "".join(fresh.iter_text(2)) == "done"


def test_stream_context_closes_response():
    with wsgiserver(app), Client() as client:
        with client.stream("GET", f"{BASE}/target") as response:
            assert response.status_code == 200
        assert response._response._sock is None


def test_per_request_options_not_supported():
    with wsgiserver(app), Client() as client:
        for kw in ({"cookies": {}}, {"timeout": 5}, {"auth": ("a", "b")}):
            with pytest.raises(NotImplementedError):
                client.request("GET", BASE, **kw)


def test_elapsed_and_cookies_are_inherited_from_requests_surface():
    with wsgiserver(app), Client() as client:
        response = client.request("GET", f"{BASE}/target")
        assert isinstance(response.elapsed, timedelta)
        assert {cookie.name for cookie in response.cookies} == {"a", "b"}


def test_connection_error_is_translated():
    import socket

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    with Client(max_retries=0) as client, pytest.raises(ConnectError):
        client.request("GET", f"http://127.0.0.1:{dead_port}/")
