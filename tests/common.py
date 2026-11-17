import sys
from contextlib import contextmanager

import gevent.pywsgi
import gevent.queue
import gevent.server


def _raise_stored_exception(exception_queue):
    """Raise a handler exception after the server has been stopped, so that
    late handler failures do not go unnoticed. An exception already propagating
    through the context manager takes precedence."""
    if not exception_queue.empty() and sys.exc_info()[0] is None:
        raise exception_queue.get()


TEST_HOST = "127.0.0.1"
TEST_PORT = 54323
LISTENER = TEST_HOST, TEST_PORT
LISTENER_URL = f"http://{TEST_HOST}:{TEST_PORT}/"
HTTPBIN_HOST = "httpbingo.org"  # this might be exchanged with a self-hosted version


@contextmanager
def server(handler):
    exception_queue = gevent.queue.Queue()

    def wrapped_handler(env, start_response):
        try:
            return handler(env, start_response)
        except Exception as e:
            exception_queue.put(e)
            raise

    server = gevent.server.StreamServer(LISTENER, handle=wrapped_handler)
    server.start()
    try:
        yield
        _raise_stored_exception(exception_queue)
    finally:
        server.stop()
        # libuv on Windows needs a loop tick to retire the accept watcher, otherwise
        # the next server on this port never accepts.
        gevent.sleep(0.001)
        _raise_stored_exception(exception_queue)


@contextmanager
def wsgiserver(handler):
    exception_queue = gevent.queue.Queue()

    def wrapped_handler(env, start_response):
        try:
            return handler(env, start_response)
        except Exception as e:
            exception_queue.put(e)
            raise

    server = gevent.pywsgi.WSGIServer(LISTENER, wrapped_handler)
    server.start()
    try:
        yield
        _raise_stored_exception(exception_queue)
    finally:
        server.stop()
        gevent.sleep(0.001)
        _raise_stored_exception(exception_queue)


@contextmanager
def header_echo_server():
    """Serve requests and collect the raw request head they carried.

    Yields a list, one entry per request, with the request head as text.
    """
    received: list[str] = []

    def handler(sock, addr):
        data = b""
        while b"\r\n\r\n" not in data:
            block = sock.recv(4096)
            if not block:
                break
            data += block
        received.append(data.decode("latin-1"))
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        sock.close()

    with server(handler):
        yield received


def parse_request_headers(head: str) -> dict[str, str]:
    """The headers of a raw request head, lowercased names, stripped values."""
    headers = {}
    for line in head.split("\r\n")[1:]:
        if not line:
            break
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return headers


def check_upload(body, headers=None, length=None):
    def wsgi_handler(env, start_response):
        assert body == env["wsgi.input"].read()
        assert env["REQUEST_METHOD"] == "POST"
        if length is not None:
            assert int(env.get("CONTENT_LENGTH")) == length
            assert len(body) == length
        if headers:
            for field, val in headers.items():
                env_key = field.upper().replace("-", "_")
                assert env[env_key] == val
                if env_key == "CONTENT_LENGTH":
                    assert len(body) == int(val)
        start_response("200 OK", [])
        return []

    return wsgi_handler
