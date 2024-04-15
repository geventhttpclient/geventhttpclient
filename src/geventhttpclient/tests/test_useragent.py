from http.cookiejar import CookieJar

import pytest

from geventhttpclient.tests.conftest import LISTENER_URL, wsgiserver
from geventhttpclient.useragent import BadStatusCode, UserAgent


@pytest.fixture
def tmp_file(tmp_path):
    fpath = tmp_path / "tmp.bin"
    with open(fpath, "wb") as f:
        f.write(b"123456789")
    return fpath


def check_upload(body, headers=None):
    def wsgi_handler(env, start_response):
        assert env["REQUEST_METHOD"] == "POST"
        assert body == env["wsgi.input"].read()
        start_response("200 OK", [])
        return []

    return wsgi_handler


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


def test_file_post(tmp_file):
    headers = {"CONTENT_LENGTH": "9", "CONTENT_TYPE": "application/octet-stream"}
    with wsgiserver(check_upload(b"123456789", headers)):
        useragent = UserAgent()
        with open(tmp_file, "rb") as body:
            useragent.urlopen(LISTENER_URL, method="POST", payload=body)


def test_multipart_post(tmp_file):
    with open(tmp_file, "a+b") as f:
        headers = {
            "CONTENT_LENGTH": "237",
            "CONTENT_TYPE": "multipart/form-data; boundary=custom_boundary",
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
                    b'Content-Disposition: form-data; name="files"\r\n'
                    b"\r\n"
                    b"file\r\n"
                    b"--custom_boundary\r\n"
                    b'Content-Disposition: form-data; name="file"; filename="report.xls"\r\n'
                    b"Content-Type: application/vnd.ms-excel\r\n"
                    b"Expires: 0\r\n"
                    b"\r\n"
                    b"\r\n"
                    b"--custom_boundary--"
                    b"\r\n"
                ),
                headers,
            )
        ):
            useragent = UserAgent()
            useragent.urlopen(LISTENER_URL, method="POST", files=files)


def test_unicode_post():
    byte_string = b"\xc8\xb9\xc8\xbc\xc9\x85"
    unicode_string = byte_string.decode("utf-8")
    headers = {
        "CONTENT_LENGTH": str(len(byte_string)),
        "CONTENT_TYPE": "text/plain; charset=utf-8",
    }
    with wsgiserver(check_upload(byte_string, headers)):
        useragent = UserAgent()
        useragent.urlopen(LISTENER_URL, method="POST", payload=unicode_string)


def test_bytes_post():
    headers = {"CONTENT_LENGTH": "5", "CONTENT_TYPE": "application/octet-stream"}
    with wsgiserver(check_upload(b"12345", headers)):
        useragent = UserAgent()
        useragent.urlopen(LISTENER_URL, method="POST", payload=b"12345")


def test_dict_post_with_content_type():
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    payload = {"foo": "bar"}
    with wsgiserver(set_cookie()):  # lazy. I just want to see that we dont crash making the request
        resp = UserAgent().urlopen(LISTENER_URL, method="POST", payload=payload, headers=headers)
        assert resp.status_code == 200


def test_redirect():
    with wsgiserver(check_redirect()):
        resp = UserAgent().urlopen(LISTENER_URL)
        assert resp.status_code == 200
        assert b"redirected" == resp.content


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
        with pytest.raises(BadStatusCode):
            with open(tmp_file, "rb") as body:
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
def test_download(tmp_path):
    url = "https://proof.ovh.net/files/1Mb.dat"
    fpath = tmp_path / url.rsplit("/", 1)[-1]
    UserAgent().download(url, fpath)
    assert fpath.stat().st_size == 2**20  # 1 MB
