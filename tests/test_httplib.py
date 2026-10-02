import gzip
import http.client
import urllib.request

import pytest

from geventhttpclient.httplib import HTTPConnection, patched
from tests.common import HTTPBIN_HOST, LISTENER, server


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


def test_response_stays_open_until_the_buffered_body_is_consumed():
    """Small responses complete within one read: the parser hands the socket
    back to the pool while the body is still buffered. urllib3's
    is_fp_closed() consults isclosed() first and must not see such a
    response as finished before the buffered body has been consumed."""
    with server(success_response):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        response = connection.getresponse()

        # the socket is already pooled at this point
        assert response._sock is None
        # ... but the buffered body must keep the stream open
        assert not response.isclosed()
        assert not response.closed

        assert response.read() == b"Hello World!"

        # only now the stream is exhausted
        assert response.isclosed()
        assert response.closed


def chunked_gzip_response(sock, addr):
    sock.recv(4096)
    body = gzip.compress(b"Hello World!")
    chunk = b"%x\r\n%s\r\n" % (len(body), body)
    sock.sendall(
        b"HTTP/1.1 200 Ok\r\n"
        b"Content-Type: text/plain\r\n"
        b"Transfer-Encoding: chunked\r\n\r\n" + chunk + b"0\r\n\r\n"
    )


def test_chunked_response_reads_body_after_socket_is_pooled():
    """Chunked responses release the socket at the terminal chunk while the
    decoded body stays buffered; reads must still return it."""
    with server(chunked_gzip_response):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        response = connection.getresponse()

        assert gzip.decompress(response.read()) == b"Hello World!"
        assert response.closed
        # no `fp` attribute: urllib3's supports_chunked_reads() keys on it and
        # would otherwise read our decoded payload as chunked wire format
        assert not hasattr(response, "fp")


def test_msg_items_are_a_reiterable_list():
    """urllib3 walks msg.items() twice when rebuilding its header dict
    (normalize, then consume): a one-shot iterator would exhaust on the
    first walk and silently drop every header, including Content-Encoding,
    leaving compressed bodies undecoded."""
    with server(success_response):
        connection = HTTPConnection(*LISTENER)
        connection.request("GET", "/")
        response = connection.getresponse()

        items = response.msg.items()
        first = list(items)  # urllib3's normalize walk
        second = list(items)  # urllib3's rebuild walk
        assert first == second
        assert ("set-cookie", "foo=bar") in first
        assert ("set-cookie", "baz=bar") in first


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
