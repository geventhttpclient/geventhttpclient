import sys
from functools import wraps
from http.client import HTTPException
from io import StringIO

import pytest

from geventhttpclient.response import HTTPResponse


def test_latin1_header_value():
    """Non-UTF-8 header bytes must parse as latin-1 instead of crashing.

    Regression test: the parser used to decode header fragments as UTF-8,
    which crashed the interpreter with a segfault on invalid bytes.
    """
    response = HTTPResponse()
    response.feed(
        b"HTTP/1.1 200 Ok\r\n"
        b'Content-Disposition: attachment; filename="h\xe4llo.txt"\r\n'
        b"Content-Length: 0\r\n\r\n"
    )
    assert response["content-disposition"] == 'attachment; filename="h\xe4llo.txt"'


def test_latin1_status_message():
    """Non-UTF-8 bytes in the status line must not crash the parser."""
    response = HTTPResponse()
    response.feed(b"HTTP/1.1 200 \xff\xfe\r\nContent-Length: 0\r\n\r\n")
    assert response.status_code == 200


def test_ascii_roundtrip():
    """Plain ASCII headers keep their exact values."""
    response = HTTPResponse()
    response.feed(b"HTTP/1.1 200 Ok\r\nContent-Type: text/plain\r\nContent-Length: 0\r\n\r\n")
    assert response["content-type"] == "text/plain"


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
        assert response.status_code, "status code expected to be parsed"
        assert response.message_begun
    except HTTPException as e:
        assert "Invalid status code" in str(e)
    else:
        assert False, "should have raised"


@wrap_refcount
def test_content_length_smuggling_cve_2024_27982():
    """CVE-2024-27982: reject obfuscated Content-Length headers (llhttp >= 6.1.1)."""
    smuggling_attempts = [
        # duplicate Content-Length headers, identical values
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 5\r\n\r\nhello",
        # duplicate Content-Length headers, conflicting values
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nContent-Length: 6\r\n\r\nhello",
        # comma separated values
        b"HTTP/1.1 200 OK\r\nContent-Length: 5, 5\r\n\r\nhello",
        # space separated values
        b"HTTP/1.1 200 OK\r\nContent-Length: 5 5\r\n\r\nhello",
        # signed value
        b"HTTP/1.1 200 OK\r\nContent-Length: +5\r\n\r\nhello",
        # Content-Length together with Transfer-Encoding
        b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
    ]
    for raw_response in smuggling_attempts:
        response = HTTPResponse()
        with pytest.raises(HTTPException):
            response.feed(raw_response)


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
