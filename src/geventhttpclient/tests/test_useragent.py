import gevent.pywsgi
import os
import pytest
import six
import tempfile

if six.PY2:
    from cookielib import CookieJar
else:
    from http.cookiejar import CookieJar

from contextlib import contextmanager
from geventhttpclient.useragent import UserAgent, BadStatusCode


@contextmanager
def wsgiserver(handler):
    server = gevent.pywsgi.WSGIServer(('127.0.0.1', 54323), handler)
    server.start()
    try:
        yield
    finally:
        server.stop()


def check_upload(body, headers=None):
    def wsgi_handler(env, start_response):
        if headers:
            # For Python 2.6 which does not have viewitems
            if six.PY2:
                env >= headers
            else:
                assert six.viewitems(env) >= six.viewitems(headers)
        assert body == env['wsgi.input'].read()
        start_response('200 OK', [])
        return []
    return wsgi_handler


def internal_server_error():
    def wsgi_handler(env, start_response):
        start_response('500 Internal Server Error', [])
        return []
    return wsgi_handler


def check_redirect():
    def wsgi_handler(env, start_response):
        path_info = env.get('PATH_INFO')
        if path_info == "/":
            start_response('301 Moved Permanently', [('Location', 'http://127.0.0.1:54323/redirected')])
            return []
        else:
            assert path_info == "/redirected"
            start_response('200 OK', [])
            return [b"redirected"]
    return wsgi_handler

def check_querystring():
    def wsgi_handler(env, start_response):
        querystring = env["QUERY_STRING"]
        start_response('200 OK', [("Content-type", "text/plaim")])
        return [querystring.encode("utf-8")]
    return wsgi_handler

def set_cookie():
    def wsgi_handler(env, start_response):
        start_response('200 OK', [('Set-Cookie', 'testcookie=testdata')])
        return []
    return wsgi_handler

def return_brotli():
    def wsgi_handler(env, start_response):
        path_info = env.get('PATH_INFO')
        if path_info == "/":
            start_response('200 OK', [("Content-Encoding", "br")])
        return [b"\x1b'\x00\x98\x04rq\x88\xa1'\xbf]\x12\xac+g!%\x98\xf4\x02\xc4\xda~)8\xba\x06xO\x11)Y\x02"]
    return wsgi_handler


def test_file_post():
    body = tempfile.NamedTemporaryFile("a+b", delete=False)
    name = body.name
    try:
        body.write(b"123456789")
        body.close()
        headers = {'CONTENT_LENGTH': '9', 'CONTENT_TYPE': 'application/octet-stream'}
        with wsgiserver(check_upload(b"123456789", headers)):
            useragent = UserAgent()
            with open(name, 'rb') as body:
                useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload=body)
    finally:
        os.remove(name)


def test_unicode_post():
    byte_string = b'\xc8\xb9\xc8\xbc\xc9\x85'
    unicode_string = byte_string.decode('utf-8')
    headers = {'CONTENT_LENGTH': str(len(byte_string)), 'CONTENT_TYPE': 'text/plain; charset=utf-8'}
    with wsgiserver(check_upload(byte_string, headers)):
        useragent = UserAgent()
        useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload=unicode_string)


def test_bytes_post():
    headers = {'CONTENT_LENGTH': '5', 'CONTENT_TYPE': 'application/octet-stream'}
    with wsgiserver(check_upload(b"12345", headers)):
        useragent = UserAgent()
        useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload=b"12345")


def test_redirect():
    with wsgiserver(check_redirect()):
        resp = UserAgent().urlopen('http://127.0.0.1:54323/')
        assert resp.status_code == 200
        assert b"redirected" == resp.content

def test_params():
    with wsgiserver(check_querystring()):
        resp = UserAgent().urlopen('http://127.0.0.1:54323/?param1=b', params={"param2":"hello"})
        assert resp.status_code == 200
        assert resp.content == b"param1=b&param2=hello"

def test_params_quoted():
    with wsgiserver(check_querystring()):
        resp = UserAgent().urlopen('http://127.0.0.1:54323/?a/b', params={"path":"/"})
        assert resp.status_code == 200
        assert resp.content == b"a/b&path=%2F"

def test_server_error_with_bytes():
    with wsgiserver(internal_server_error()):
        useragent = UserAgent()
        with pytest.raises(BadStatusCode):
            useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload=b"12345")


def test_server_error_with_unicode():
    with wsgiserver(internal_server_error()):
        useragent = UserAgent()
        with pytest.raises(BadStatusCode):
            useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload=u"12345")


def test_server_error_with_file():
    body = tempfile.NamedTemporaryFile("a+b", delete=False)
    name = body.name
    try:
        body.write(b"123456789")
        body.close()
        with wsgiserver(internal_server_error()):
            useragent = UserAgent()
            with pytest.raises(BadStatusCode):
                with open(name, 'rb') as body:
                    useragent.urlopen('http://127.0.0.1:54323/', method='POST', payload=body)
    finally:
        os.remove(name)


def test_cookiejar():
    with wsgiserver(set_cookie()):
        useragent = UserAgent(cookiejar=CookieJar())
        assert b"" == useragent.urlopen('http://127.0.0.1:54323/').read()


def test_brotli_response():
    with wsgiserver(return_brotli()):
        resp = UserAgent().urlopen('http://127.0.0.1:54323/', params={"path":"/"})
        assert resp.status_code == 200
        assert resp.content == b"https://github.com/gwik/geventhttpclient"
