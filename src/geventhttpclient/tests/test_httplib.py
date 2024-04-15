import http.client
import urllib.request

import pytest

from geventhttpclient.httplib import HTTPConnection, patched
from geventhttpclient.tests.conftest import HTTPBIN_HOST, LISTENER, server


def wrong_response_status_line(sock, addr):
    sock.recv(4096)
    sock.sendall(b"HTTP/1.1 apfais df0 asdf\r\n\r\n")


def test_httplib_exception():
    with server(wrong_response_status_line):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        with pytest.raises(http.client.HTTPException):
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


def test_patched():
    assert http.client.HTTPResponse.__module__ == "http.client"
    assert http.client.HTTPConnection.__module__ == "http.client"
    with patched():
        assert http.client.HTTPResponse.__module__ == "geventhttpclient.httplib"
        assert http.client.HTTPConnection.__module__ == "geventhttpclient.httplib"
    assert http.client.HTTPResponse.__module__ == "http.client"
    assert http.client.HTTPConnection.__module__ == "http.client"


@pytest.mark.network
@pytest.mark.parametrize("url", [f"http://{HTTPBIN_HOST}", "https://github.com"])
def test_urllib_request(url):
    with patched():
        content = urllib.request.urlopen(url).read()
        assert content
        assert b"body" in content
