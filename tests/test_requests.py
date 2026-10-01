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


def test_ok_is_true_for_statuses_below_400():
    assert _response(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n").ok
    assert _response(b"HTTP/1.1 302 X\r\nLocation: /x\r\nContent-Length: 0\r\n\r\n").ok
    assert not _response(b"HTTP/1.1 404 X\r\nContent-Length: 0\r\n\r\n").ok


def test_reason_comes_from_the_status_line():
    response = _response(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
    assert response.reason == "Not Found"


def test_bool_follows_own_ok():
    """``bool(response)`` is True for any status below 400, False otherwise."""
    ok = _response(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    assert bool(ok) is True
    assert (not ok) is False
    bad = _response(b"HTTP/1.1 503 X\r\nContent-Length: 0\r\n\r\n")
    assert bool(bad) is False
    redirect = _response(b"HTTP/1.1 304 X\r\nContent-Length: 0\r\n\r\n")
    assert bool(redirect) is True


def test_close_releases_the_connection():
    """``close`` is the requests-style alias that delegates to ``release``."""
    response = _response(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    calls: list[int] = []
    response._response.release = lambda: calls.append(1)  # type: ignore[attr-defined]
    response.close()
    response.close()
    assert calls == [1, 1]


def test_is_permanent_redirect_only_for_301_and_308():
    """301 and 308 are permanent; 302 and 307 are temporary; no Location, no redirect."""
    line = b"HTTP/1.1 %d X\r\nLocation: /next\r\nContent-Length: 0\r\n\r\n"
    assert _response(line % 301).is_permanent_redirect
    assert _response(line % 308).is_permanent_redirect
    assert not _response(line % 302).is_permanent_redirect
    assert not _response(line % 307).is_permanent_redirect
    assert not _response(b"HTTP/1.1 301 X\r\nContent-Length: 0\r\n\r\n").is_permanent_redirect


def test_encoding_comes_from_content_type():
    response = _response(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: 0\r\n\r\n"
    )
    assert response.encoding == "utf-8"
    response = _response(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=\"ISO-8859-1\"\r\nContent-Length: 0\r\n\r\n"
    )
    assert response.encoding == "iso-8859-1"
    response = _response(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 0\r\n\r\n"
    )
    assert response.encoding is None
    response = _response(
        b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    )
    assert response.encoding is None


def test_links_parses_an_rfc_5988_header():
    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b'Link: <https://example.com/page/2>; rel="next", '
        b'<https://example.com/page/9>; rel="last"; title="Last Page"\r\n'
        b"Content-Length: 0\r\n\r\n"
    )
    links = _response(raw).links
    assert links["next"] == {"url": "https://example.com/page/2", "rel": "next"}
    assert links["last"] == {
        "url": "https://example.com/page/9",
        "rel": "last",
        "title": "Last Page",
    }


def test_links_is_empty_when_no_link_header():
    response = _response(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
    assert response.links == {}
    response = _response(b"HTTP/1.1 200 OK\r\nLink: \r\nContent-Length: 0\r\n\r\n")
    assert response.links == {}


def test_links_keys_by_url_when_no_rel_is_given():
    """requests keys links without a rel parameter by their url."""
    raw = (
        b"HTTP/1.1 200 OK\r\n"
        b'Link: <https://example.com/no-rel>; title="No rel", '
        b'<https://example.com/next>; rel="next"\r\n'
        b"Content-Length: 0\r\n\r\n"
    )
    links = _response(raw).links
    assert links["https://example.com/no-rel"] == {
        "url": "https://example.com/no-rel",
        "title": "No rel",
    }
    assert links["next"] == {"url": "https://example.com/next", "rel": "next"}
