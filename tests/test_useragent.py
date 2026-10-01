import traceback
from http.cookiejar import CookieJar
from io import BytesIO

import pytest

from geventhttpclient.header import Headers
from geventhttpclient.useragent import (
    BadStatusCode,
    CompatRequest,
    UserAgent,
    _encode_multipart_formdata,
)
from tests.common import HTTPBIN_HOST, LISTENER_URL, check_upload, wsgiserver


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
    assert request.headers.get("content-length") == 3
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
