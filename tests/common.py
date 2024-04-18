from contextlib import contextmanager

import gevent.pywsgi
import gevent.queue
import gevent.server

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
        if not exception_queue.empty():
            raise exception_queue.get()
    finally:
        server.stop()


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
        if not exception_queue.empty():
            raise exception_queue.get()
    finally:
        server.stop()


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
