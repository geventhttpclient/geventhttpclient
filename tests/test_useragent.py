import traceback
import urllib.request
from email.message import Message
from http.cookiejar import CookieJar, DefaultCookiePolicy
from io import BytesIO

import gevent.server
import pytest

from geventhttpclient.header import Headers
from geventhttpclient.useragent import (
    BadStatusCode,
    CompatRequest,
    RetriesExceeded,
    UnrewoundBodyError,
    UnsupportedRedirectSchemeError,
    UserAgent,
    _encode_multipart_formdata,
)
from tests.common import (
    HTTPBIN_HOST,
    LISTENER,
    LISTENER_URL,
    TEST_PORT,
    check_upload,
    server,
    wsgiserver,
)


@pytest.fixture
def tmp_file(tmp_path):
    fpath = tmp_path / "tmp.bin"
    with open(fpath, "wb") as f:
        f.write(b"123456789")
    return fpath


def internal_server_error():
    def wsgi_handler(env, start_response):
        start_response("500 Internal Server Error", [])
        return []

    return wsgi_handler


def check_redirect():
    def wsgi_handler(env, start_response):
        path_info = env.get("PATH_INFO")
        if path_info == "/":
            start_response("301 Moved Permanently", [("Location", LISTENER_URL + "redirected")])
            return []
        else:
            assert path_info == "/redirected"
            start_response("200 OK", [])
            return [b"redirected"]

    return wsgi_handler


def check_redirect_308():
    def wsgi_handler(env, start_response):
        path_info = env.get("PATH_INFO")
        if path_info == "/":
            start_response(
                "308 Permanent Redirect", [("Location", LISTENER_URL + "redirected_308")]
            )
            return []
        else:
            assert path_info == "/redirected_308"
            start_response("200 OK", [])
            return [b"redirected_308"]

    return wsgi_handler


def check_querystring():
    def wsgi_handler(env, start_response):
        querystring = env["QUERY_STRING"]
        start_response("200 OK", [("Content-type", "text/plaim")])
        return [querystring.encode("utf-8")]

    return wsgi_handler


def set_cookie():
    def wsgi_handler(env, start_response):
        start_response("200 OK", [("Set-Cookie", "testcookie=testdata")])
        return []

    return wsgi_handler


def set_cookie_401():
    def wsgi_handler(env, start_response):
        start_response("401 Unauthorized", [("Set-Cookie", "testcookie=testdata")])
        return []

    return wsgi_handler


def return_brotli():
    def wsgi_handler(env, start_response):
        path_info = env.get("PATH_INFO")
        if path_info == "/":
            start_response("200 OK", [("Content-Encoding", "br")])
        return [
            b"\x1b'\x00\x98\x04rq\x88\xa1'\xbf]\x12\xac+g!%\x98\xf4\x02\xc4\xda~)8\xba\x06xO\x11)Y\x02"
        ]

    return wsgi_handler


def test_unicode_post():
    byte_string = b"\xc8\xb9\xc8\xbc\xc9\x85"
    unicode_string = byte_string.decode("utf-8")
    headers = {
        "Content-Length": str(len(byte_string)),
        "Content-Type": "text/plain; charset=utf-8",
    }
    with wsgiserver(check_upload(byte_string, headers)):
        useragent = UserAgent()
        useragent.urlopen(LISTENER_URL, method="POST", payload=unicode_string)


def test_bytes_post():
    headers = {"Content-Length": "5", "Content-Type": "application/octet-stream"}
    with wsgiserver(check_upload(b"12345", headers)):
        useragent = UserAgent()
        useragent.urlopen(LISTENER_URL, method="POST", payload=b"12345")


def test_dict_post_with_content_type():
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {"foo": "bar"}
    with wsgiserver(set_cookie()):  # lazy. I just want to see that we dont crash making the request
        resp = UserAgent().urlopen(LISTENER_URL, method="POST", payload=payload, headers=headers)
        assert resp.status_code == 200


def test_file_post(tmp_file):
    headers = {"Content-Length": "9", "Content-Type": "application/octet-stream"}
    with wsgiserver(check_upload(b"123456789", headers)):
        useragent = UserAgent()
        with open(tmp_file, "rb") as body:
            useragent.urlopen(LISTENER_URL, method="POST", payload=body)


def test_multipart_file(tmp_file):
    with open(tmp_file, "rb") as f:
        headers = {
            "Content-Length": "173",
            "Content-Type": "multipart/form-data; boundary=custom_boundary",
        }
        files = {
            "file": (
                "report.xls",
                f,
                "application/vnd.ms-excel",
                {"Expires": "0"},
                "custom_boundary",
            )
        }

        with wsgiserver(
            check_upload(
                (
                    b"--custom_boundary\r\n"
                    b'Content-Disposition: form-data; name="file"; filename="report.xls"\r\n'
                    b"Content-Type: application/vnd.ms-excel\r\n"
                    b"Expires: 0\r\n"
                    b"\r\n"
                    b"123456789\r\n"
                    b"--custom_boundary--"
                    b"\r\n"
                ),
                headers,
            )
        ):
            useragent = UserAgent()
            useragent.urlopen(LISTENER_URL, method="POST", files=files)


def test_multipart_mixed(tmp_file):
    with open(tmp_file, "rb") as f:
        headers = {
            "Content-Length": "248",
            "Content-Type": "multipart/form-data; boundary=custom_boundary",
        }
        files = {
            "file": (
                "report.xls",
                f,
                "application/vnd.ms-excel",
                {"Expires": "0"},
                "custom_boundary",
            )
        }

        with wsgiserver(
            check_upload(
                (
                    b"--custom_boundary\r\n"
                    b'Content-Disposition: form-data; name="bla"\r\n'
                    b"\r\n"
                    b"sometext\r\n"
                    b"--custom_boundary\r\n"
                    b'Content-Disposition: form-data; name="file"; filename="report.xls"\r\n'
                    b"Content-Type: application/vnd.ms-excel\r\n"
                    b"Expires: 0\r\n"
                    b"\r\n"
                    b"123456789\r\n"
                    b"--custom_boundary--"
                    b"\r\n"
                ),
                headers,
            )
        ):
            useragent = UserAgent()
            useragent.urlopen(LISTENER_URL, method="POST", files=files, bla="sometext")


def test_multipart_boundary_none_in_5_tuple():
    """A 5-tuple with boundary=None must yield a consistent random boundary."""
    body, content_type = _encode_multipart_formdata(
        {"file": ("a.txt", BytesIO(b"hi"), None, None, None)}, None
    )
    boundary = content_type.rsplit("=", 1)[1]
    assert boundary != "None"
    assert body.startswith(b"--%s\r\n" % boundary.encode())
    assert body.endswith(b"--%s--\r\n" % boundary.encode())


def test_multipart_two_custom_boundaries_first_wins():
    """Header and body must use the same boundary when files disagree."""
    body, content_type = _encode_multipart_formdata(
        [
            ("f1", ("a.txt", BytesIO(b"hi"), None, None, "first")),
            ("f2", ("b.txt", BytesIO(b"ho"), None, None, "second")),
        ],
        None,
    )
    assert content_type == "multipart/form-data; boundary=first"
    assert body.startswith(b"--first\r\n")
    assert body.endswith(b"--first--\r\n")
    assert b"second" not in body


def test_multipart_too_long_tuple_raises():
    """File tuples with more than 5 elements must raise a ValueError."""
    with pytest.raises(ValueError):
        _encode_multipart_formdata(
            {"file": ("a.txt", BytesIO(b"hi"), None, None, "b", "extra")}, None
        )


def test_redirect():
    with wsgiserver(check_redirect()):
        resp = UserAgent().urlopen(LISTENER_URL)
        assert resp.status_code == 200
        assert b"redirected" == resp.content


def _read_request_target(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(8192)
        if not chunk:
            break
        data += chunk
    return data.split(b" ", 2)[1]


@pytest.mark.parametrize(
    ("reference", "expected_target"),
    [
        ("resource;session=abc", "/resource;session=abc"),
        (
            "dir;prefix=value/resource;session=abc?query=1",
            "/dir;prefix=value/resource;session=abc?query=1",
        ),
        (
            "resource;name=two words?token=a%2Fb%3Bc#ignored",
            "/resource;name=two%20words?token=a%2Fb%3Bc",
        ),
    ],
)
def test_path_parameters_reach_server(reference, expected_target):
    received = []

    def handler(sock, address):
        received.append(_read_request_target(sock))
        sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    with server(handler), UserAgent() as useragent:
        resp = useragent.urlopen(LISTENER_URL + reference)
        assert resp.status_code == 200
        assert resp.content == b"ok"
    assert received == [expected_target.encode("ascii")]


@pytest.mark.parametrize(
    ("location", "expected_target"),
    [
        ("target;session=new?query=1", "/dir/target;session=new?query=1"),
        (
            LISTENER_URL + "target;token=a%2Fb%3Bc?query=1#ignored",
            "/target;token=a%2Fb%3Bc?query=1",
        ),
        ("?query=2", "/dir/start;session=old?query=2"),
    ],
)
def test_redirect_path_parameters_reach_server(location, expected_target):
    received = []

    def handler(sock, address):
        received.append(_read_request_target(sock))
        if len(received) == 1:
            sock.sendall(
                (
                    f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode("ascii")
            )
        else:
            sock.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    with server(handler), UserAgent() as useragent:
        resp = useragent.urlopen(LISTENER_URL + "dir/start;session=old?original=1")
        assert resp.status_code == 200
        assert resp.content == b"ok"
    assert received == [
        b"/dir/start;session=old?original=1",
        expected_target.encode("ascii"),
    ]


def test_redirect_drops_authorization_across_origins():
    """A caller-supplied Authorization header must not travel to a redirect
    target on a different origin (RFC 9110 section 15.4 asks implementations
    to consider removing it; browsers and requests do the same)."""
    req = CompatRequest("https://example.com/", headers=Headers({"authorization": "Basic x"}))
    req.redirect(302, "https://other.example.com/sub")
    assert "authorization" not in req.headers


def test_redirect_keeps_authorization_for_same_origin():
    req = CompatRequest("https://example.com/", headers=Headers({"authorization": "Basic x"}))
    req.redirect(302, "https://example.com/sub")
    assert req.headers["authorization"] == "Basic x"


def test_redirect_drops_authorization_on_scheme_downgrade():
    """Same host, but https -> http: a different origin, credentials out."""
    req = CompatRequest("https://example.com/", headers=Headers({"authorization": "Basic x"}))
    req.redirect(302, "http://example.com/sub")
    assert "authorization" not in req.headers


def test_redirect_307_rewinds_seekable_payload():
    payload = BytesIO(b"123456789")
    payload.read(3)
    # explicit empty Headers: redirect() drops cookies, which needs a real
    # mapping (pre-existing requirement, unchecked before this change)
    req = CompatRequest("https://example.com/", method="POST", headers=Headers(), payload=payload)
    req.redirect(307, "/other")
    assert payload.tell() == 0


def test_redirect_307_rejects_unrewindable_payload():
    """A consumed iterator cannot be resent; failing loudly beats shipping a
    truncated body under the original length. The same holds for a partially
    consumed generator."""
    req = CompatRequest("https://example.com/", method="POST", payload=iter([b"abc"]))
    with pytest.raises(UnrewoundBodyError):
        req.redirect(307, "/other")

    def two_chunks():
        yield b"a"
        yield b"b"

    generator = two_chunks()
    next(generator)  # half of the body already consumed
    req = CompatRequest("https://example.com/", method="POST", headers=Headers(), payload=generator)
    with pytest.raises(UnrewoundBodyError):
        req.redirect(307, "/other")


def test_redirect_307_rejects_stream_whose_seek_fails():
    """A BufferedReader on a pipe (subprocess.Popen.stdout) has a seek
    attribute, but seek(0) raises OSError (ESPIPE) - not rewindable either,
    and it must surface as UnrewoundBodyError, not as a raw OSError."""

    class PipeLike:
        def seek(self, offset: int, whence: int = 0) -> int:
            raise OSError(29, "Illegal seek")

    req = CompatRequest(
        "https://example.com/", method="POST", headers=Headers(), payload=PipeLike()
    )
    with pytest.raises(UnrewoundBodyError):
        req.redirect(307, "/other")


def test_redirect_307_resends_the_full_body():
    received = []

    def handler(env, start_response):
        # the BytesIO payload has no fileno, so the client streams it chunked
        # and CONTENT_LENGTH is absent; read() drains the de-chunked stream
        body = env["wsgi.input"].read()
        received.append(body)
        if env["PATH_INFO"] == "/":
            start_response("307 Temporary Redirect", [("Location", "target")])
            return []
        start_response("200 OK", [])
        return [body]

    with wsgiserver(handler):
        resp = UserAgent().urlopen(LISTENER_URL, method="POST", payload=BytesIO(b"123456789"))
        assert resp.status_code == 200
        assert resp.content == b"123456789"
        assert received == [b"123456789", b"123456789"]


def test_redirect_refuses_foreign_schemes():
    """A redirect must stay within http(s); HTTPClient.from_url would degrade
    anything else to plain http. The request stays on the original URL."""
    req = CompatRequest("https://example.com/", headers=Headers())
    with pytest.raises(UnsupportedRedirectSchemeError):
        req.redirect(302, "ftp://other.example.com/file")
    assert req.url == "https://example.com/"


def test_redirect_to_foreign_scheme_is_refused_end_to_end():
    def handler(env, start_response):
        start_response("302 Found", [("Location", "ftp://other.example.com/file")])
        return []

    with wsgiserver(handler), pytest.raises(UnsupportedRedirectSchemeError):
        UserAgent().urlopen(LISTENER_URL)


def test_redirect_keeps_head_method():
    """RFC 9110 section 15.4: method changes follow the status code's
    semantics; HEAD survives 301/302/303 like in requests and browsers."""
    for code in (301, 302, 303):
        req = CompatRequest("https://example.com/", method="HEAD", headers=Headers())
        req.redirect(code, "/other")
        assert req.method == "HEAD"
        assert req.payload is None


def test_redirect_rewrites_post_to_get():
    for code in (301, 302, 303):
        req = CompatRequest("https://example.com/", method="POST", headers=Headers(), payload=b"x")
        req.redirect(code, "/other")
        assert req.method == "GET"
        assert req.payload is None


def test_redirect_keeps_head_method_end_to_end():
    methods = []

    def handler(env, start_response):
        methods.append(env["REQUEST_METHOD"])
        if env["PATH_INFO"] == "/":
            start_response("301 Moved Permanently", [("Location", "target")])
            return []
        start_response("200 OK", [])
        return [b"done"]

    with wsgiserver(handler):
        resp = UserAgent().urlopen(LISTENER_URL, method="HEAD")
        assert resp.status_code == 200
    assert methods == ["HEAD", "HEAD"]


def test_list_header_value_reaches_the_wire_as_two_field_lines():
    """End to end through the UserAgent: a Mapping with a list value arrives
    as two field lines on the wire, never as the Python repr of the list.
    A raw socket server sees the actual request head - gevent's WSGI server
    would silently keep only the last of the duplicate lines."""
    received = []

    def handle(sock, address):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
        received.append(data)
        body = b"ok"
        sock.sendall(
            b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        )

    server = gevent.server.StreamServer(LISTENER, handle)
    server.start()
    try:
        resp = UserAgent().urlopen(LISTENER_URL, headers={"X-Multi": ["a", "b"]})
        assert resp.status_code == 200
    finally:
        server.stop()
    head = received[0].decode("latin-1")
    assert "X-Multi: a\r\nX-Multi: b\r\n" in head
    assert "['a', 'b']" not in head


def test_is_unverifiable_follows_the_redirect_chain():
    """RFC 2965 section 3.3: any request produced by an automatic redirect
    is unverifiable from the user's perspective - same origin or not, like
    urllib.request's redirect handler."""
    req = CompatRequest("https://example.com/", headers=Headers())
    assert req.is_unverifiable() is False
    req.redirect(302, "https://example.com/first-hop")
    assert req.is_unverifiable() is True


def test_strict_policy_skips_cookies_set_after_a_redirect():
    """End to end: the cookie of the redirecting response stays verifiable
    and is stored. The cookie of the redirect target is unverifiable AND
    third-party (different host string), so a strict policy refuses to
    store it. localhost and 127.0.0.1 are the same server here, but
    different hosts for the policy."""

    def handler(env, start_response):
        if env["PATH_INFO"] == "/":
            start_response(
                "302 Found",
                [
                    ("Location", f"http://localhost:{TEST_PORT}/target"),
                    ("Set-Cookie", "first=1; Path=/"),
                ],
            )
            return []
        start_response("200 OK", [("Set-Cookie", "second=2; Path=/")])
        return [b""]

    jar = CookieJar(policy=DefaultCookiePolicy(strict_ns_unverifiable=True))
    with wsgiserver(handler):
        resp = UserAgent(cookiejar=jar).urlopen(LISTENER_URL)
        assert resp.status_code == 200
    assert {cookie.name for cookie in jar} == {"first"}


def test_redirect_308():
    with wsgiserver(check_redirect_308()):
        resp = UserAgent().urlopen(LISTENER_URL)
        assert resp.status_code == 200
        assert b"redirected_308" == resp.content


def test_params():
    with wsgiserver(check_querystring()):
        resp = UserAgent().urlopen(LISTENER_URL + "?param1=b", params={"param2": "hello"})
        assert resp.status_code == 200
        assert resp.content == b"param1=b&param2=hello"


def test_params_quoted():
    with wsgiserver(check_querystring()):
        resp = UserAgent().urlopen(LISTENER_URL + "?a/b", params={"path": "/"})
        assert resp.status_code == 200
        assert resp.content == b"a/b&path=%2F"


def test_server_error_with_bytes():
    with wsgiserver(internal_server_error()):
        useragent = UserAgent()
        with pytest.raises(BadStatusCode):
            useragent.urlopen(LISTENER_URL, method="POST", payload=b"12345")


def test_server_error_with_unicode():
    with wsgiserver(internal_server_error()):
        useragent = UserAgent()
        with pytest.raises(BadStatusCode):
            useragent.urlopen(LISTENER_URL, method="POST", payload="12345")


def test_server_error_with_file(tmp_file):
    with wsgiserver(internal_server_error()):
        useragent = UserAgent()
        with pytest.raises(BadStatusCode), open(tmp_file, "rb") as body:
            useragent.urlopen(LISTENER_URL, method="POST", payload=body)


def test_cookiejar():
    with wsgiserver(set_cookie()):
        useragent = UserAgent(cookiejar=CookieJar())
        assert b"" == useragent.urlopen(LISTENER_URL).read()


def test_cookiejar_response_error():
    with wsgiserver(set_cookie_401()):
        useragent = UserAgent(cookiejar=CookieJar())
        with pytest.raises(BadStatusCode):
            assert b"" == useragent.urlopen(LISTENER_URL)

        assert (
            next(cookie for cookie in useragent.cookiejar if cookie.name == "testcookie").value
            == "testdata"
        )


def test_brotli_response():
    with wsgiserver(return_brotli()):
        resp = UserAgent().urlopen(LISTENER_URL, params={"path": "/"})
        assert resp.status_code == 200
        assert resp.content == b"https://github.com/gwik/geventhttpclient"


@pytest.mark.network
def test_no_form_encoded_header():
    url = f"https://{HTTPBIN_HOST}/headers"
    hdrs = Headers(UserAgent().urlopen(url).json()["headers"])
    print(hdrs)
    assert "content-type" not in hdrs
    assert "content-length" not in hdrs


@pytest.mark.network
def test_download(tmp_path):
    url = "https://proof.ovh.net/files/1Mb.dat"
    fpath = tmp_path / url.rsplit("/", 1)[-1]
    UserAgent().download(url, fpath)
    assert fpath.stat().st_size == 2**20  # 1 MB


@pytest.mark.network
def test_httpbin_multipart():
    """Sent a request body with mixed form data:

    --custom_boundary_12345
    Content-Disposition: form-data; name="bla"

    sometext
    --custom_boundary_12345
    Content-Disposition: form-data; name="file"; filename="report.xls"
    Content-Type: application/vnd.ms-excel
    Expires: 0

    1234567890
    --custom_boundary_12345--
    """

    custom_boundary = "custom_boundary_12345"
    files = {
        "file": (
            "report.xls",
            b"1234567890",
            "application/vnd.ms-excel",
            {"Expires": "0"},
            custom_boundary,
        )
    }
    resp = UserAgent().urlopen(
        f"http://{HTTPBIN_HOST}/post", method="POST", files=files, bla="sometext"
    )
    rjson = resp.json()
    request_lines = rjson["data"].splitlines()
    assert request_lines[0] == f"--{custom_boundary}"
    assert request_lines[-1] == f"--{custom_boundary}--"
    assert rjson["headers"]["Content-Type"] == [f"multipart/form-data; boundary={custom_boundary}"]
    assert rjson["files"]["file"] == ["1234567890"]
    assert rjson["form"]["bla"] == ["sometext"]


def test_make_request_without_headers():
    """A caller that hands us headers=None must not crash."""
    request = UserAgent()._make_request("http://example.com/", method="GET", headers=None)
    assert request.method == "GET"
    assert len(request.headers) == 0
    assert request.headers.get("content-type") is None


def test_make_request_still_describes_the_payload():
    """The headers invented for a headerless request still get the payload ones."""
    request = UserAgent()._make_request(
        "http://example.com/", method="POST", headers=None, payload={"a": "b"}
    )
    assert request.headers.get("content-type") == "application/x-www-form-urlencoded; charset=utf-8"
    assert request.headers.get("content-length") == "3"
    assert request.payload == b"a=b"


def test_make_request_uses_the_agents_request_type():
    """The method on the agent exists to pass on its own request type."""

    class CustomRequest(CompatRequest):
        pass

    class CustomAgent(UserAgent):
        request_type = CustomRequest

    made = CustomAgent()._make_request("http://example.com/", method="GET", headers=None)
    assert type(made) is CustomRequest
    assert type(UserAgent()._make_request("http://example.com/")) is CompatRequest


def test_handle_error_keeps_the_traceback_of_the_error():
    """The error hook must not lean on the ambient exception state.

    From inside an ``except`` clause both spellings give the same traceback;
    called once the clause is left, the frames the error came through have
    to stay attached to it.
    """

    def failing():
        raise ValueError("raised deep")

    with pytest.raises(ValueError) as deep:
        failing()

    # the except clause above is left by now
    with pytest.raises(ValueError) as raised:
        UserAgent()._handle_error(deep.value)

    assert "in failing" in "".join(traceback.format_tb(raised.value.__traceback__))


class ResponseInfo:
    """Enough of a response for the cookie jar: headers carrying a Set-Cookie."""

    def __init__(self, set_cookie: str) -> None:
        message = Message()
        message["Set-Cookie"] = set_cookie
        self._message = message

    def info(self) -> Message:
        return self._message


def test_compat_request_is_a_urllib_request():
    """The base class provides the urllib surface with our real values in it."""
    request = CompatRequest("https://example.com:8443/p?a=1", method="post", payload=b"x=1")
    assert isinstance(request, urllib.request.Request)
    assert request.get_method() == "POST"
    assert request.data == b"x=1"
    assert (request.type, request.host, request.selector) == (
        "https",
        "example.com:8443",
        "/p?a=1",
    )
    # get_host is our legacy reading, without the port; the host attribute
    # of the base class carries the netloc
    assert request.get_host() == "example.com"
    assert request.get_type() == "https"


def test_cookiejar_adds_its_cookies_into_the_headers():
    """A secure cookie on https needs request.type, and it must land in the
    headers our client sends, not in an unredirected dict it never reads."""
    request = CompatRequest("https://example.com/p", headers=Headers())
    jar = CookieJar()
    jar.extract_cookies(ResponseInfo("k=v; Path=/; Domain=example.com; Secure"), request)
    assert list(jar)
    jar.add_cookie_header(request)
    assert request.get_header("Cookie") == "k=v"
    assert request.headers.get("cookie") == "k=v"


def test_full_url_assignment_reparses_the_request():
    """Assigning full_url updates url, the split, and the parsed attributes."""
    request = CompatRequest("http://example.com/p")
    request.full_url = "https://other.example:8443/q"
    assert request.url == "https://other.example:8443/q"
    assert request.url_split.host == "other.example"
    # host is the netloc, the base class keeps the port in it
    assert request.host == "other.example:8443"
    assert (request.type, request.selector) == ("https", "/q")


def empty_body_handler(attempts: list):
    def handler(env, start_response):
        attempts.append(1)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b""]

    return handler


def test_post_is_not_retried_on_empty_response():
    """RFC 9110 section 9.2.2: a non-idempotent request must not be retried
    automatically - the first attempt may have been executed already."""
    attempts: list = []
    with wsgiserver(empty_body_handler(attempts)), pytest.raises(RetriesExceeded):
        UserAgent(max_retries=3).urlopen(LISTENER_URL, method="POST", to_string=True)
    assert len(attempts) == 1


def test_get_is_still_retried_on_empty_response():
    attempts: list = []
    with wsgiserver(empty_body_handler(attempts)), pytest.raises(RetriesExceeded):
        UserAgent(max_retries=2).urlopen(LISTENER_URL, to_string=True)
    assert len(attempts) == 3
