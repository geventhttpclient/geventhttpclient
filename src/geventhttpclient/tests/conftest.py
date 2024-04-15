import gevent.monkey

gevent.monkey.patch_all()  # make sure all tests run monkey patched

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
