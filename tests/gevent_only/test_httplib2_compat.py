"""Compatibility tests for the httplib2 wrapper.

The wrapper pools real httplib2.Http clients, so httplib2 itself is
installed as a dev dependency and these tests skip without it. The
binding of httplib2 onto our gevent connection classes happens at the
import of geventhttpclient.httplib2.
"""

import time

import gevent.pool
import pytest

import geventhttpclient.httplib
from geventhttpclient import httplib2
from tests.common import LISTENER_URL, wsgiserver

httplib2_module = pytest.importorskip("httplib2")


def echo_handler(env, start_response):
    if env["PATH_INFO"] == "/redirect":
        start_response("302 Found", [("Location", "/target")])
        return []
    body = env["wsgi.input"].read() if env["REQUEST_METHOD"] == "POST" else b""
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [body if body else b"body of " + env["PATH_INFO"].encode()]


def slow_echo_handler(env, start_response):
    gevent.sleep(0.3)
    start_response("200 OK", [])
    return [b"body of " + env["PATH_INFO"].encode()]


def assert_same_result(expected_path, response, content):
    assert isinstance(response, httplib2_module.Response)
    assert response.status == 200
    assert response.fromcache is False
    assert response["content-type"] == "text/plain"
    assert content == b"body of " + expected_path.encode()


def test_request_matches_a_plain_httplib2_client():
    with wsgiserver(echo_handler):
        plain = httplib2_module.Http()
        reference = plain.request(LISTENER_URL + "some/path")
        pooled = httplib2.Http(concurrency=3).request(LISTENER_URL + "some/path")
    for response, content in (reference, pooled):
        assert_same_result("/some/path", response, content)


def test_post_sends_body_and_headers():
    with wsgiserver(echo_handler):
        for client in (httplib2_module.Http(), httplib2.Http()):
            response, content = client.request(
                LISTENER_URL + "upload",
                method="POST",
                body=b"payload",
                headers={"X-Custom": "present"},
            )
            assert response.status == 200
            assert content == b"payload"


def test_redirects_are_followed_and_kept_in_previous():
    with wsgiserver(echo_handler):
        for client in (httplib2_module.Http(), httplib2.Http()):
            response, content = client.request(LISTENER_URL + "redirect")
            assert response.status == 200
            assert content == b"body of /target"
            assert response.previous.status == 302


def test_not_found_comes_back_as_a_response():
    def not_found_handler(env, start_response):
        start_response("404 Not Found", [("Content-Type", "text/plain")])
        return [b"gone"]

    with wsgiserver(not_found_handler):
        for client in (httplib2_module.Http(), httplib2.Http()):
            response, content = client.request(LISTENER_URL + "missing")
            assert response.status == 404
            assert content == b"gone"


def test_constructor_kwargs_reach_the_pooled_clients():
    with wsgiserver(echo_handler):
        response, content = httplib2.Http(timeout=5, concurrency=2).request(LISTENER_URL + "kwargs")
        assert_same_result("/kwargs", response, content)


def test_pooled_requests_run_concurrently():
    with wsgiserver(slow_echo_handler):
        client = httplib2.Http(concurrency=3)
        group = gevent.pool.Pool(size=3)
        started = time.monotonic()
        jobs = [
            group.spawn(client.request, LISTENER_URL + path) for path in ("one", "two", "three")
        ]
        group.join()
        duration = time.monotonic() - started
    assert [job.value[1] for job in jobs] == [
        b"body of /one",
        b"body of /two",
        b"body of /three",
    ]
    # three sequential requests would take about 0.9s, concurrency must
    # keep the wall time clearly below it even on slow runners
    assert duration < 0.8, f"requests were serialized ({duration:.2f}s)"


def test_the_binding_runs_httplib2_on_the_gevent_connections():
    assert issubclass(
        httplib2_module.HTTPConnectionWithTimeout,
        geventhttpclient.httplib.HTTPConnection,
    )
    assert issubclass(
        httplib2_module.HTTPSConnectionWithTimeout,
        geventhttpclient.httplib.HTTPSConnection,
    )
    # the http.client patch is scoped to the import of the wrapper module
    import http.client

    assert http.client.HTTPConnection is not geventhttpclient.httplib.HTTPConnection


def test_configuration_boundary_of_the_pool():
    """Mutating configuration attributes cannot be spread over the pooled
    clients, configuration belongs into the constructor."""
    client = httplib2.Http(concurrency=2)
    assert not hasattr(client, "add_credentials")
    assert not hasattr(client, "follow_redirects")
