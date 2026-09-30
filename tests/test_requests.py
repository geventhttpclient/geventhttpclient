import pytest

from geventhttpclient.header import Headers
from geventhttpclient.requests import RequestsResponse, Session
from geventhttpclient.response import HTTPResponse
from tests.common import HTTPBIN_HOST


@pytest.mark.network
def test_no_form_encode_header():
    url = f"https://{HTTPBIN_HOST}/headers"
    hdrs = Headers(Session().get(url).json()["headers"])
    print(hdrs)
    assert "content-type" not in hdrs
    assert "content-length" not in hdrs


def _response(raw: bytes) -> RequestsResponse:
    """The bytes of a response, through the parser, no socket involved."""
    response = HTTPResponse()
    response.feed(raw)
    return RequestsResponse(response)


def test_is_redirect_only_for_codes_the_client_follows():
    """Only 301, 302, 303, 307 and 308 are redirects, not the whole 3xx block."""
    status_line = b"HTTP/1.1 %d X\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n"
    found = {code: _response(status_line % code).is_redirect for code in range(300, 310)}
    assert found == {
        300: False,
        301: True,
        302: True,
        303: True,
        304: False,  # may name a Location, but nothing moved
        305: False,
        306: False,
        307: True,
        308: True,
        309: False,
    }


def test_is_redirect_needs_a_location_header():
    assert not _response(b"HTTP/1.1 302 X\r\nContent-Length: 0\r\n\r\n").is_redirect
