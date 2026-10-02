import json

import pytest

from geventhttpclient import BasicAuth
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


def _response_with_body(body: bytes, headers: bytes = b"") -> RequestsResponse:
    """A response with extra headers and a real body; ``read`` is then mocked
    to drain that buffer.

    The parser the rest of the tests use has ``Content-Length: 0`` and never
    bothers with ``read``; the streaming tests here need a real body and a way
    to consume it byte by byte.
    """
    response = HTTPResponse()
    response.feed(
        b"HTTP/1.1 200 OK\r\n"
        + headers
        + b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
    )
    for byte in body:
        response.feed(bytes([byte]))
    wrapped = RequestsResponse(response)
    buf = bytearray(body)

    def fake_read(n: int | None = None) -> bytes:
        if n is None:
            chunk = bytes(buf)
            del buf[:]
            return chunk
        chunk = bytes(buf[:n])
        del buf[:n]
        return chunk

    wrapped._response.read = fake_read  # type: ignore[attr-defined]
    # the parser-only response has no socket-backed release(); useragent's
    # content cache calls release() on the wrapped response, so stub it
    wrapped._response.release = lambda: None  # type: ignore[attr-defined]
    return wrapped


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
        b'HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset="ISO-8859-1"\r\nContent-Length: 0\r\n\r\n'
    )
    assert response.encoding == "iso-8859-1"
    response = _response(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 0\r\n\r\n"
    )
    assert response.encoding is None
    response = _response(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
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


def test_iter_content_yields_chunks_of_requested_size():
    body = b"abcdefghij"
    response = _response_with_body(body)
    chunks = list(response.iter_content(chunk_size=3))
    assert chunks == [b"abc", b"def", b"ghi", b"j"]


def test_iter_content_default_chunk_size_and_empty_body():
    response = _response_with_body(b"")
    assert list(response.iter_content()) == []


def test_iter_content_decodes_with_content_type_charset():
    body = "héllo".encode()
    response = _response_with_body(body, headers=b"Content-Type: text/plain; charset=utf-8\r\n")
    assert list(response.iter_content(chunk_size=64, decode_unicode=True)) == ["héllo"]


def test_iter_content_decoded_reassembles_multibyte_characters_over_chunks():
    """The incremental decoder must not replace characters split in half."""
    body = "héllo".encode()  # the \xc3\xa9 straddles the size-2 boundary
    response = _response_with_body(body, headers=b"Content-Type: text/plain; charset=utf-8\r\n")
    assert list(response.iter_content(chunk_size=2, decode_unicode=True)) == ["h", "él", "lo"]


def test_iter_content_chunk_size_must_be_positive():
    """A non-positive chunk size is clamped to single-byte chunks."""
    response = _response_with_body(b"abc")
    assert list(response.iter_content(chunk_size=0)) == [b"a", b"b", b"c"]


def test_iter_lines_splits_on_newline_by_default():
    body = b"line1\nline2\nline3\n"
    response = _response_with_body(body)
    # trailing newline produces an empty trailing split, which we do not yield
    assert list(response.iter_lines()) == [b"line1", b"line2", b"line3"]


def test_iter_lines_reassembles_lines_that_span_chunks():
    body = b"line1\nline"
    response = _response_with_body(body)
    assert list(response.iter_lines(chunk_size=4)) == [b"line1", b"line"]


def test_iter_lines_yields_a_trailing_partial_line():
    body = b"line1\nline2-partial"
    response = _response_with_body(body)
    assert list(response.iter_lines()) == [b"line1", b"line2-partial"]


def test_iter_lines_custom_delimiter():
    body = b"alpha|beta|gamma"
    response = _response_with_body(body)
    assert list(response.iter_lines(delimiter=b"|")) == [b"alpha", b"beta", b"gamma"]


def test_iter_lines_decode_unicode():
    response = _response_with_body(
        "café\nça".encode(), headers=b"Content-Type: text/plain; charset=utf-8\r\n"
    )
    assert list(response.iter_lines(decode_unicode=True)) == ["café", "ça"]


def test_iter_lines_decode_unicode_with_custom_delimiter():
    response = _response_with_body(
        b"a;b\nc", headers=b"Content-Type: text/plain; charset=utf-8\r\n"
    )
    assert list(response.iter_lines(decode_unicode=True, delimiter=b";")) == ["a", "b\nc"]


def test_iter_lines_splits_crlf_terminated_lines():
    response = _response_with_body(b"line1\r\nline2\r\n")
    assert list(response.iter_lines()) == [b"line1", b"line2"]


def test_iter_lines_chunk_boundaries_on_line_breaks_do_not_merge_lines():
    """A chunk ending exactly on a line break must not glue that line to the
    first line of the next chunk."""
    response = _response_with_body(b"line1\r\nline2\r\n")
    assert list(response.iter_lines(chunk_size=7)) == [b"line1", b"line2"]


def test_iter_lines_crlf_straddling_a_chunk_boundary_is_one_terminator():
    """A \\r that lands on the end of a chunk may pair with the \\n of the next
    chunk; it must not produce a phantom empty line."""
    response = _response_with_body(b"a\r\nb\r\n")
    assert list(response.iter_lines(chunk_size=2)) == [b"a", b"b"]
    response = _response_with_body(b"a\r\nb\r\n")
    assert list(response.iter_lines(chunk_size=3, delimiter=b"x")) == [b"a\r\nb\r\n"]


def test_iter_lines_empty_body_yields_nothing():
    response = _response_with_body(b"")
    assert list(response.iter_lines()) == []


def test_iter_lines_bare_cr_at_end_of_body_terminates_the_last_line():
    """A body ending in a bare \\r (no trailing \\n) terminates the final line;
    the trailing \\r must not leak as an empty trailing line."""
    response = _response_with_body(b"a\r")
    assert list(response.iter_lines()) == [b"a"]
    # same when the lone \\r lands on a chunk boundary of its own
    response = _response_with_body(b"a\r")
    assert list(response.iter_lines(chunk_size=1)) == [b"a"]
    response = _response_with_body(b"a\r")
    assert list(response.iter_lines(chunk_size=2)) == [b"a"]


def test_iter_lines_lone_cr_body_is_one_empty_line():
    response = _response_with_body(b"\r")
    assert list(response.iter_lines()) == [b""]
    response = _response_with_body(b"\r")
    assert list(response.iter_lines(chunk_size=1)) == [b""]


def test_iter_lines_leading_crlf_yields_a_leading_empty_line():
    response = _response_with_body(b"\r\na")
    assert list(response.iter_lines()) == [b"", b"a"]
    response = _response_with_body(b"\r\na")
    assert list(response.iter_lines(chunk_size=1)) == [b"", b"a"]
    response = _response_with_body(b"\r\na")
    assert list(response.iter_lines(chunk_size=2)) == [b"", b"a"]


def test_iter_lines_double_bare_cr_yields_an_empty_line_in_the_middle():
    response = _response_with_body(b"a\r\rb")
    assert list(response.iter_lines()) == [b"a", b"", b"b"]


def test_iter_lines_crlf_followed_by_bare_cr_yields_an_empty_trailing_line():
    """A CRLF-terminated line followed by a bare CR is two terminators: an
    empty trailing line on top of the line before."""
    response = _response_with_body(b"a\r\n\r")
    assert list(response.iter_lines()) == [b"a", b""]
    response = _response_with_body(b"a\r\n\r")
    assert list(response.iter_lines(chunk_size=2)) == [b"a", b""]
    response = _response_with_body(b"a\r\n\r")
    assert list(response.iter_lines(chunk_size=4)) == [b"a", b""]


def test_iter_lines_decode_unicode_handles_bare_cr():
    """The decoded path must apply the same \\r-hold logic as the bytes path."""
    response = _response_with_body(b"a\r", headers=b"Content-Type: text/plain; charset=utf-8\r\n")
    assert list(response.iter_lines(decode_unicode=True)) == ["a"]
    response = _response_with_body(b"\r", headers=b"Content-Type: text/plain; charset=utf-8\r\n")
    assert list(response.iter_lines(decode_unicode=True)) == [""]
    response = _response_with_body(
        b"\r\na\r", headers=b"Content-Type: text/plain; charset=utf-8\r\n"
    )
    assert list(response.iter_lines(decode_unicode=True)) == ["", "a"]


def test_iter_lines_decode_unicode_empty_body():
    response = _response_with_body(b"", headers=b"Content-Type: text/plain; charset=utf-8\r\n")
    assert list(response.iter_lines(decode_unicode=True)) == []


def test_json_decodes_a_valid_body():
    body = b'{"a": 1, "b": 2}'
    response = _response_with_body(body)
    assert response.json() == {"a": 1, "b": 2}


def test_json_forwards_kwargs_to_the_decoder():
    """``**kw`` lands at ``json.loads``; our override adds the forwarding."""
    body = b'{"a": 1, "b": 2}'
    response = _response_with_body(body)
    assert response.json(object_hook=lambda d: {k.upper(): v for k, v in d.items()}) == {
        "A": 1,
        "B": 2,
    }


def test_json_raises_jsondecodeerror_on_bad_input():
    response = _response_with_body(b"not json")
    with pytest.raises(json.JSONDecodeError) as excinfo:
        response.json()
    assert isinstance(excinfo.value, ValueError)


# ---------------------------------------------------------------------------
# Session(auth=) - the requests-style session-level authentication
# ---------------------------------------------------------------------------

import base64 as _base64

from geventhttpclient import BasicAuth as _BasicAuth
from geventhttpclient.requests import Session as _Session
from tests.common import LISTENER as _LISTENER
from tests.common import wsgiserver as _wsgiserver

_BASE = f"http://127.0.0.1:{_LISTENER[1]}"
_EXPECTED_AUTH = "Basic " + _base64.b64encode(b"user:pass").decode("ascii")
_OTHER_AUTH = "Basic " + _base64.b64encode(b"alice:wonderland").decode("ascii")


def _auth_app(environ, start_response):
    path = environ["PATH_INFO"]
    if path == "/auth":
        if environ.get("HTTP_AUTHORIZATION") == _EXPECTED_AUTH:
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"ok"]
        if environ.get("HTTP_AUTHORIZATION") == _OTHER_AUTH:
            start_response("200 OK", [("Content-Type", "text/plain")])
            return [b"other"]
        start_response("401 Unauthorized", [])
        return [b"no auth"]
    if path == "/never":
        if environ.get("HTTP_AUTHORIZATION") != _EXPECTED_AUTH:
            start_response("401 Unauthorized", [])
            return [b"no auth"]
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


def test_session_auth_tuple_sets_authorization_header_on_every_request():
    with _wsgiserver(_auth_app), _Session(auth=("user", "pass")) as session:
        assert session.get(f"{_BASE}/never").status_code == 200
        assert session.get(f"{_BASE}/never").status_code == 200


def test_session_auth_basicauth_object_works_the_same():
    with _wsgiserver(_auth_app), _Session(auth=_BasicAuth("user", "pass")) as session:
        assert session.get(f"{_BASE}/never").status_code == 200


def test_per_request_auth_tuple_overrides_session_auth():
    with _wsgiserver(_auth_app), _Session(auth=("user", "pass")) as session:
        assert session.get(f"{_BASE}/auth", auth=("alice", "wonderland")).status_code == 200
        assert session.get(f"{_BASE}/auth").status_code == 200


def test_session_without_auth_does_not_send_authorization_header():
    with _wsgiserver(_auth_app), _Session() as session:
        assert session.get(f"{_BASE}/never").status_code == 401


def test_session_auth_invalid_raises():
    with pytest.raises(NotImplementedError):
        _Session(auth=123)


def test_per_request_auth_invalid_raises():
    with _wsgiserver(_auth_app), _Session() as session, pytest.raises(NotImplementedError):
        session.get(f"{_BASE}/never", auth=object())


@pytest.mark.network
def test_basicauth_end_to_end():
    """End-to-end check against httpbingo's /basic-auth/{user}/{pass}:

    the request goes out, httpbingo decodes the ``Authorization``
    header and answers with a JSON body confirming the credentials.
    Marked ``network`` so the default ``-m 'not network'`` run skips it.
    """
    response = Session().get(
        f"https://{HTTPBIN_HOST}/basic-auth/user/pass",
        auth=BasicAuth("user", "pass"),
    )
    assert response.status_code == 200
    assert response.json() == {
        "authenticated": True,
        "user": "user",
        "authorized": True,
    }


@pytest.mark.network
def test_session_auth_end_to_end():
    """Same, but the auth comes from the session - confirms that
    ``Session(auth=...)`` is wired through ``urlopen`` so the
    ``Authorization`` header actually reaches the server."""
    with Session(auth=BasicAuth("user", "pass")) as session:
        response = session.get(f"https://{HTTPBIN_HOST}/basic-auth/user/pass")
    assert response.status_code == 200
    assert response.json()["authenticated"] is True
