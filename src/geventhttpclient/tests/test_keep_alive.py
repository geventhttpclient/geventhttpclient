from geventhttpclient._parser import HTTPParseError
from geventhttpclient.response import HTTPResponse
import pytest


def test_simple():
    response = HTTPResponse()
    response.feed("""HTTP/1.1 200 Ok\r\nContent-Length: 0\r\n\r\n""")
    assert response.headers_complete
    assert response.message_complete
    assert response.should_keep_alive()
    assert not response.should_close()
    assert response.status_code == 200


def test_simple_with_body():
    response = HTTPResponse()
    response.feed("""HTTP/1.1 200 Ok\r\nContent-Length: 5\r\n\r\n12345""")
    assert response.headers_complete
    assert response.message_complete
    assert response.should_keep_alive()
    assert not response.should_close()
    assert response.status_code == 200


def test_keep_alive_http_10_without_header():
    response = HTTPResponse()
    response.feed("""HTTP/1.0 200 Ok\r\n\r\n""")
    response.feed("")
    assert response.headers_complete
    assert response.message_complete
    assert not response.should_keep_alive()
    assert response.should_close()
    assert response.status_code == 200


def test_keep_alive_http_10_with_header():
    response = HTTPResponse()
    response.feed("HTTP/1.0 200 Ok\r\nConnection: keep-alive\r\nContent-Length: 5\r\n\r\n12345")
    assert response.headers_complete
    assert response.message_complete
    assert response.should_keep_alive()
    assert not response.should_close()
    assert response.status_code == 200


def test_keep_alive_http_10_closed():
    response = HTTPResponse()
    response.feed("HTTP/1.0 200 Ok\r\nConnection: close\r\nContent-Length: 5\r\n\r\n12345")
    assert response.headers_complete
    assert response.message_complete
    assert not response.should_keep_alive()
    assert response.should_close()
    assert response.status_code == 200


def test_keep_alive_bodyless_response_with_body():
    response = HTTPResponse(method='HEAD')
    response.feed("HTTP/1.1 200 Ok\r\n\r\n")
    assert response.message_complete
    assert response.should_keep_alive()

    response = HTTPResponse(method='HEAD')
    with pytest.raises(HTTPParseError):
        response.feed(
            """HTTP/1.1 200 Ok\r\nContent-Length: 10\r\n\r\n0123456789""")
    assert not response.should_keep_alive()
    assert response.should_close()


def test_keep_alive_bodyless_10x_request_with_body():
    response = HTTPResponse()
    response.feed("""HTTP/1.1 100 Continue\r\n\r\n""")
    assert response.should_keep_alive()

    response = HTTPResponse()
    response.feed("""HTTP/1.1 100 Continue\r\nTransfer-Encoding: chunked\r\n\r\n""")
    assert response.should_keep_alive()
    assert response.should_close()

def test_close_connection_and_no_content_length():
    response = HTTPResponse()
    response.feed("HTTP/1.1 200 Ok\r\n"
                "Connection: close\r\n\r\n"
                "Hello World!")
    assert response._body_buffer == bytearray(b"Hello World!")
    assert not response.should_keep_alive()
    assert response.should_close()


def test_close_connection_with_content_length():
    response = HTTPResponse()
    response.feed("HTTP/1.1 200 Ok\r\nContent-length: 5\r\nConnection: close\r\n\r\n12345")
    assert response._body_buffer == bytearray(b"12345")
    assert not response.should_keep_alive()
    assert response.should_close()
