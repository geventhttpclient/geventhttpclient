import sys
from functools import wraps
from http.client import HTTPException
from io import StringIO

import pytest

from geventhttpclient.response import HTTPResponse

RESPONSE = (
    "HTTP/1.1 301 Moved Permanently\r\nLocation: http://www.google.fr/\r\n"
    "Content-Type: text/html; charset=UTF-8\r\n"
    "Date: Thu, 13 Oct 2011 15:03:12 GMT\r\n"
    "Expires: Sat, 12 Nov 2011 15:03:12 GMT\r\n"
    "Cache-Control: public, max-age=2592000\r\n"
    "Server: gws\r\nContent-Length: 218\r\n"
    "X-XSS-Protection: 1; mode=block\r\n\r\n"
    '<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n'
    "<TITLE>301 Moved</TITLE></HEAD><BODY>\n"
    '<H1>301 Moved</H1>\nThe document has moved\n<A HREF="http://www.google.fr/">here</A>.\r\n'
    "</BODY></HTML>\r\n"
)

# borrowed from gevent
# sys.gettotalrefcount is available only with python built with debug flag on
gettotalrefcount = getattr(sys, "gettotalrefcount", None)


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
            for _ in range(4):
                d = gettotalrefcount()
                method(*args, **kwargs)
                if "urlparse" in sys.modules:
                    sys.modules["urlparse"].clear_cache()
                d = gettotalrefcount() - d
                deltas.append(d)
                if deltas[-1] == 0:
                    break
            else:
                raise AssertionError(f"refcount increased by {deltas!r}")
        finally:
            gc.collect()
            gc.enable()

    return wrapped


@wrap_refcount
def test_parse():
    parser = HTTPResponse()
    parser.feed(RESPONSE)
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
        ("Cache-Control", "public, max-age=2592000"),
        ("Content-Length", "218"),
        ("Content-Type", "text/html; charset=UTF-8"),
        ("Date", "Thu, 13 Oct 2011 15:03:12 GMT"),
        ("Expires", "Sat, 12 Nov 2011 15:03:12 GMT"),
        ("Location", "http://www.google.fr/"),
        ("Server", "gws"),
        ("X-XSS-Protection", "1; mode=block"),
    ]


@wrap_refcount
def test_parse_error():
    response = HTTPResponse()
    try:
        response.feed("HTTP/1.1 asdf\r\n\r\n")
        response.feed("")
        assert response.status_code, 0
        assert response.message_begun
    except HTTPException as e:
        assert "Invalid response status" in str(e)
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
        raise RuntimeError("error")

    response._on_body = on_body
    with pytest.raises(RuntimeError):
        response.feed(RESPONSE)


@wrap_refcount
def test_on_message_begin():
    response = HTTPResponse()

    def on_message_begin():
        raise RuntimeError("error")

    response._on_message_begin = on_message_begin
    with pytest.raises(RuntimeError):
        response.feed(RESPONSE)
