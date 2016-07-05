import six
from geventhttpclient.response import HTTPResponse
if six.PY3:
    from http.client import HTTPException
    from io import StringIO
else:
    from httplib import HTTPException
    from cStringIO import StringIO
import pytest

from functools import wraps
import sys
from six.moves import xrange


RESPONSE = 'HTTP/1.1 301 Moved Permanently\r\nLocation: http://www.google.fr/\r\nContent-Type: text/html; charset=UTF-8\r\nDate: Thu, 13 Oct 2011 15:03:12 GMT\r\nExpires: Sat, 12 Nov 2011 15:03:12 GMT\r\nCache-Control: public, max-age=2592000\r\nServer: gws\r\nContent-Length: 218\r\nX-XSS-Protection: 1; mode=block\r\n\r\n<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n<TITLE>301 Moved</TITLE></HEAD><BODY>\n<H1>301 Moved</H1>\nThe document has moved\n<A HREF="http://www.google.fr/">here</A>.\r\n</BODY></HTML>\r\n'

# borrowed from gevent
# sys.gettotalrefcount is available only with python built with debug flag on
gettotalrefcount = getattr(sys, 'gettotalrefcount', None)


def wrap_refcount(method):
    if gettotalrefcount is None:
        return method
    @wraps(method)
    def wrapped(*args, **kwargs):
        import gc
        gc.disable()
        gc.collect()
        deltas = []
        d = None
        try:
            for _ in xrange(4):
                d = gettotalrefcount()
                method(*args, **kwargs)
                if 'urlparse' in sys.modules:
                    sys.modules['urlparse'].clear_cache()
                d = gettotalrefcount() - d
                deltas.append(d)
                if deltas[-1] == 0:
                    break
            else:
                raise AssertionError('refcount increased by %r' % (deltas, ))
        finally:
            gc.collect()
            gc.enable()
    return wrapped

@wrap_refcount
def test_parse():
    parser = HTTPResponse()
    assert parser.feed(RESPONSE), len(RESPONSE)
    assert parser.message_begun
    assert parser.headers_complete
    assert parser.message_complete

@wrap_refcount
def test_parse_small_blocks():
    parser = HTTPResponse()
    parser.feed(RESPONSE)
    response = StringIO(RESPONSE)
    while not parser.message_complete:
        data = response.read(10)
        parser.feed(data)

    assert parser.message_begun
    assert parser.headers_complete
    assert parser.message_complete
    assert parser.should_keep_alive()
    assert parser.status_code == 301
    assert sorted(parser.items()) == [
        ('cache-control', 'public, max-age=2592000'),
        ('content-length', '218'),
        ('content-type', 'text/html; charset=UTF-8'),
        ('date', 'Thu, 13 Oct 2011 15:03:12 GMT'),
        ('expires', 'Sat, 12 Nov 2011 15:03:12 GMT'),
        ('location', 'http://www.google.fr/'),
        ('server', 'gws'),
        ('x-xss-protection', '1; mode=block'),
    ]

@wrap_refcount
def test_parse_error():
    response =  HTTPResponse()
    try:
        response.feed("HTTP/1.1 asdf\r\n\r\n")
        response.feed("")
        assert response.status_code, 0
        assert response.message_begun
    except HTTPException as e:
        assert 'invalid HTTP status code' in str(e)
    else:
        assert False, "should have raised"

@wrap_refcount
def test_incomplete_response():
    response = HTTPResponse()
    response.feed("""HTTP/1.1 200 Ok\r\nContent-Length:10\r\n\r\n1""")
    with pytest.raises(HTTPException):
        response.feed("")
    assert response.should_keep_alive()
    assert response.should_close()

@wrap_refcount
def test_response_too_long():
    response = HTTPResponse()
    data = """HTTP/1.1 200 Ok\r\nContent-Length:1\r\n\r\ntoolong"""
    with pytest.raises(HTTPException):
        response.feed(data)

@wrap_refcount
def test_on_body_raises():
    response = HTTPResponse()

    def on_body(buf):
        raise RuntimeError('error')

    response._on_body = on_body
    with pytest.raises(RuntimeError):
        response.feed(RESPONSE)

@wrap_refcount
def test_on_message_begin():
    response = HTTPResponse()

    def on_message_begin():
        raise RuntimeError('error')

    response._on_message_begin = on_message_begin
    with pytest.raises(RuntimeError):
        response.feed(RESPONSE)


