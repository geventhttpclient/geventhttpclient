from contextlib import contextmanager
import gevent.pywsgi
from geventhttpclient.useragent import UserAgent


@contextmanager
def wsgiserver(handler):
    server = gevent.pywsgi.WSGIServer(('127.0.0.1', 54323), handler)
    server.start()
    try:
        yield
    finally:
        server.stop()


def check_upload(body, body_length):
    def wsgi_handler(env, start_response):
        assert int(env.get('CONTENT_LENGTH')) == body_length
        assert body == env['wsgi.input'].read()
        start_response('200 OK', [])
        return []
    return wsgi_handler


def check_redirect():

    def wsgi_handler(env, start_response):
        if env.get('PATH_INFO') == "/":
            start_response('301 Moved Permanently', [('Location', 'http://127.0.0.1:54323/redirected')])
            return []
        else:
            assert env.get('PATH_INFO') == "/redirected"
            start_response('200 OK', [])
            return [b"redirected"]
    return wsgi_handler


def test_string_post():
    with wsgiserver(check_upload(b"12345", 5)):
        useragent = UserAgent()
        useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload="12345")


def test_redirect():
    with wsgiserver(check_redirect()):
        useragent = UserAgent()
        assert b"redirected" == useragent.urlopen('http://127.0.0.1:54323/').read()
