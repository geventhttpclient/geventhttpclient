import urllib.request
from contextlib import contextmanager
from http.client import HTTPException

import gevent.server
import pytest

from geventhttpclient.httplib import HTTPConnection

LISTENER = "127.0.0.1", 54323


@contextmanager
def server(handler):
    server = gevent.server.StreamServer(LISTENER, handle=handler)
    server.start()
    try:
        yield
    finally:
        server.stop()


def wrong_response_status_line(sock, addr):
    sock.recv(4096)
    sock.sendall(b"HTTP/1.1 apfais df0 asdf\r\n\r\n")


def test_httplib_exception():
    with server(wrong_response_status_line):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        with pytest.raises(HTTPException):
            connection.getresponse()


def success_response(sock, addr):
    sock.recv(4096)
    sock.sendall(
        b"HTTP/1.1 200 Ok\r\n"
        b"Content-Type: text/plain\r\n"
        b"Set-Cookie: foo=bar\r\n"
        b"Set-Cookie: baz=bar\r\n"
        b"Content-Length: 12\r\n\r\n"
        b"Hello World!"
    )


def test_success_response():
    with server(success_response):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.should_keep_alive()
        assert response.message_complete
        assert not response.should_close()
        assert response.read().decode() == "Hello World!"
        assert response.content_length == 12


def test_msg():
    with server(success_response):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        response = connection.getresponse()

        assert response.msg["Set-Cookie"] == "foo=bar, baz=bar"
        assert response.msg["Content-Type"] == "text/plain"


@pytest.mark.network
@pytest.mark.parametrize("url", ["http://httpbingo.org", "https://github.com"])
def test_urllib_request(url):
    content = urllib.request.urlopen(url).read()
    assert content
    assert b"body" in content
