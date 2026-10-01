import random
import string
from datetime import datetime
from email.message import Message
from http.cookiejar import CookieJar
from urllib.request import Request

import pytest

from geventhttpclient.header import Headers
from geventhttpclient.response import HTTPResponse

CUR_YEAR = datetime.now().year
LAST_YEAR = CUR_YEAR - 1
NEXT_YEAR = CUR_YEAR + 1
MULTI_COOKIE_RESPONSE = f"""
HTTP/1.1 200 OK
Server: nginx
Date: Fri, 21 Sep {CUR_YEAR} 18:49:35 GMT
Content-Type: text/html; charset=windows-1251
Connection: keep-alive
X-Powered-By: PHP/5.2.17
Set-Cookie: bb_lastvisit=1348253375; expires=Sat, 21-Sep-{NEXT_YEAR} 18:49:35 GMT; path=/
Set-Cookie: bb_lastactivity=0; expires=Sat, 21-Sep-{NEXT_YEAR} 18:49:35 GMT; path=/
Cache-Control: private
Pragma: private
Set-Cookie: bb_sessionhash=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_referrerid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_userid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_password=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_lastvisit=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_lastactivity=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_threadedmode=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_userstyleid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_languageid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_fbaccesstoken=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_fbprofilepicurl=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=/
Set-Cookie: bb_sessionhash=abcabcabcabcabcabcabcabcabcabcab; path=/; HttpOnly
Set-Cookie: tapatalk_redirect3=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_sessionhash=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utma=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utmb=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utmc=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utmz=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: vbulletin_collapse=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_referrerid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_userid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_password=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_lastvisit=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_lastactivity=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_threadedmode=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_userstyleid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_languageid=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_fbaccesstoken=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_fbprofilepicurl=deleted; expires=Thu, 22-Sep-{LAST_YEAR} 18:49:34 GMT; path=1; domain=forum.somewhere.com
Content-Encoding: gzip
Content-Length: 26186

""".lstrip().replace("\n", "\r\n")
# Do not remove the final empty line!


def test_create_from_kwargs():
    h = Headers(ab=1, cd=2, ef=3, gh=4)
    assert len(h) == 4
    assert "ab" in h


def test_create_from_iterator():
    h = Headers((x, x * 5) for x in string.ascii_lowercase)
    assert len(h) == len(string.ascii_lowercase)


def test_create_from_dict():
    h = Headers({"ab": 1, "cd": 2, "ef": 3, "gh": 4})
    assert len(h) == 4
    assert "ab" in h


def test_create_from_list():
    h = Headers([("ab", "A"), ("cd", "B"), ("cookie", "C"), ("cookie", "D"), ("cookie", "E")])
    assert len(h) == 5
    assert "ab" in h
    assert len(h["cookie"]) == 3
    assert h["cookie"][0] == "C"
    assert h["cookie"][-1] == "E"


def test_retrieve():
    h = Headers([("ab", "A"), ("cd", "B"), ("cookie", "C"), ("cookie", "D"), ("cookie", "E")])
    for key, ref in {"ab": "A", "cd": "B", "cookie": ["C", "D", "E"]}.items():
        assert h[key] == ref
        assert h.get(key) == ref
        assert h.pop(key) == ref
        assert key not in h


def test_case_insensitivity():
    h = Headers({"Content-Type": "text/plain"})
    h.add("Content-Encoding", "utf8")
    for val in ("content-type", "content-encoding"):
        assert val.upper() in h
        assert val.lower() in h
        assert val.capitalize() in h
        assert h.get(val.lower()) == h.get(val.upper()) == h.get(val.capitalize())
        del h[val.upper()]
        assert val.lower() not in h


def test_preserve_case():
    h = Headers(Cookie="C", COOKIE="D", cookie="E", asdf="E")
    assert len(h) == 4
    assert h["cookie"] == ["C", "D", "E"]
    assert list(h.items()) == [("Cookie", "C"), ("COOKIE", "D"), ("cookie", "E"), ("asdf", "E")]


def test_update_preserve_case():
    h = Headers()
    h.update(COOKIE="A", Cookie="C")
    assert list(h.items()) == [("Cookie", "C")]
    h = Headers()
    h.update({"Cookie": "C", "COOKIE": "D", "cookiE": "E"})
    assert list(h.items()) == [("cookiE", "E")]


def test_read_multiple_header():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    headers = parser._headers_index
    assert len(headers["set-cookie"]) == MULTI_COOKIE_RESPONSE.count("Set-Cookie")
    assert headers["set-cookie"][0].startswith("bb_lastvisit")
    assert headers["set-cookie"][-1].startswith("bb_fbprofilepicurl")


def test_cookielib_compatibility():
    cj = CookieJar()
    request = Request("https://forum.somewhere.com")
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    valid_cookie_count = sum(
        1
        for line in MULTI_COOKIE_RESPONSE.splitlines()
        if line.startswith("Set-Cookie") and str(LAST_YEAR) not in line
    )
    valid_cookies = cj.make_cookies(parser, request)
    assert len(valid_cookies) == valid_cookie_count
    cj.extract_cookies(parser, request)
    assert len(list(cj)) == valid_cookie_count


def test_compatibility_with_previous_api_read():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    for single_item in (
        "content-encoding",
        "content-type",
        "content-length",
        "cache-control",
        "connection",
    ):
        assert isinstance(parser[single_item], str)
        assert isinstance(parser.get(single_item), str)


def test_compatibility_with_previous_api_write():
    h = Headers()
    h["asdf"] = "jklm"
    h["asdf"] = "dfdf"
    # Lists only if necessary
    assert h["asdf"] == "dfdf"


def test_copy():
    def rnd_txt(length):
        return "".join(random.choice(string.ascii_letters) for _ in range(length))

    h = Headers((rnd_txt(10), rnd_txt(50)) for _ in range(100))
    c = h.copy()
    assert h is not c
    assert len(h) == len(c)
    assert set(h.keys()) == set(c.keys())
    assert h == c
    assert type(h) is type(c)
    for _ in range(100):
        rnd_key = rnd_txt(9)
        c[rnd_key] = rnd_txt(10)
        assert rnd_key in c
        assert rnd_key not in h


def test_fieldname_string_enforcement():
    with pytest.raises(Exception):
        Headers({3: 3})
    h = Headers()
    with pytest.raises(Exception):
        h[3] = 5
    with pytest.raises(Exception):
        h.add(3, 4)
    with pytest.raises(Exception):
        del h[3]


def test_header_replace():
    d = Headers()
    d["Content-Type"] = "text/plain"
    d["content-type"] = "text/html"
    assert d["content-type"] == "text/html"


def test_formatting():
    h = Headers(asdf="ddd", ASDF="fff", AsDf="asdfasdf")
    assert str(h) == "asdf: ddd\nASDF: fff\nAsDf: asdfasdf"


def test_compat_dict():
    h = Headers(D="asdf")
    h.add("E", "d")
    h.add("E", "f")
    h.add("Cookie", "d")
    h.add("Cookie", "e")
    h.add("Cookie", "f")
    d = h.compatible_dict()

    for x in ("Cookie", "D", "E"):
        assert x in d
    assert d["D"] == "asdf"
    assert d["E"] == "d, f"
    assert d["Cookie"] == "d, e, f"


def test_extend_with_keys():
    msg = Message()
    msg["Foo"] = "bar"
    msg["Baz"] = "qux"

    h = Headers()
    h.extend(msg)
    assert h["Foo"] == "bar"
    assert h["Baz"] == "qux"


def test_keys_and_values_views():
    """values() reads the decoded header values, the keys view matches
    case-insensitively like the containment test on the headers themselves."""
    headers = Headers()
    headers.add("Accept", "text/plain")
    headers.add("accept", "text/html")
    assert list(headers.keys()) == ["accept"]
    # the view itself must match case-insensitively, hence the noqa
    assert "ACCEPT" in headers.keys()  # noqa: SIM118
    assert list(headers.values()) == [["text/plain", "text/html"]]


def test_dict_level_writes_lower_their_keys():
    """Even the raw dict level APIs must not sneak unlowered keys into the
    storage, and setdefault reads and writes through the accessors."""
    headers = Headers()
    assert headers.setdefault("X-A", "first") == "first"
    assert headers.setdefault("X-A", "second") == "first"
    headers |= {"X-B": "second"}
    assert list(headers.keys()) == ["x-a", "x-b"]
    assert headers.getlist("x-a") == ["first"]
    assert headers.get("x-b") == "second"
    from_keys = Headers.fromkeys(["X-D"], "fourth")
    assert from_keys.get("x-d") == "fourth"
    assert list(from_keys.keys()) == ["x-d"]


def test_or_merge_returns_headers_and_leaves_the_left_side_alone():
    headers = Headers({"X-A": "first"})
    merged = headers | {"X-B": "second"}
    assert isinstance(merged, Headers)
    assert list(merged.keys()) == ["x-a", "x-b"]
    assert merged.get("x-a") == "first"
    assert headers.get("x-b") is None


def test_update_from_headers_replaces_whole_multi_line_fields():
    """Updating from a Headers instance replaces the entire field content;
    multi line fields must not collapse to their last line."""
    target = Headers({"X-Other": "x"})
    target.update(Headers({"X-Multi": ["a", "b"]}))
    assert target.getlist("x-multi") == ["a", "b"]
    assert target.getlist("x-other") == ["x"]
    assert list(target.items()) == [("X-Other", "x"), ("X-Multi", "a"), ("X-Multi", "b")]


def test_update_overwrites_a_field_with_a_multi_line_value():
    target = Headers({"X-Multi": "old"})
    target.update(Headers({"X-Multi": ["a", "b"]}))
    assert target.getlist("x-multi") == ["a", "b"]
    assert list(target.items()) == [("X-Multi", "a"), ("X-Multi", "b")]


def test_create_from_mapping_with_list_value():
    """A list value is one field line per element (RFC 9110 section 5.2),
    never a single line carrying the Python repr of the list."""
    h = Headers({"X-Multi": ["a", "b"]})
    assert h["x-multi"] == ["a", "b"]
    assert h.getlist("x-multi") == ["a", "b"]
    assert list(h.items()) == [("X-Multi", "a"), ("X-Multi", "b")]


def test_add_with_list_value_appends_one_line_per_element():
    h = Headers({"X-Multi": "a"})
    h.add("X-Multi", ["b", "c"])
    h.add("X-Multi", "d")
    assert h.getlist("x-multi") == ["a", "b", "c", "d"]


def test_update_with_list_value_replaces_previous_lines():
    h = Headers({"X-Multi": "old"})
    h.update({"X-Multi": ["a", "b"]})
    assert h.getlist("x-multi") == ["a", "b"]
    assert list(h.items()) == [("X-Multi", "a"), ("X-Multi", "b")]


def test_setitem_with_tuple_value():
    h = Headers()
    h["X-Multi"] = ("a", "b")
    assert h.getlist("x-multi") == ["a", "b"]
    assert list(h.items()) == [("X-Multi", "a"), ("X-Multi", "b")]
