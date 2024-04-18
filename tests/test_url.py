import pytest

from geventhttpclient.url import URL

url_full = "http://gevent.org/subdir/file.py?param=value&other=true#frag"
url_path_only = "/path/to/something?param=value&other=true"


def test_simple_url():
    url = URL(url_full)
    assert url.path == "/subdir/file.py"
    assert url.host == "gevent.org"
    assert url.port == 80
    assert url.query == "param=value&other=true"
    assert url.fragment == "frag"


def test_path_only():
    url = URL(url_path_only)
    assert url.host == ""
    assert url.port is None
    assert url.path == "/path/to/something"
    assert url.query == "param=value&other=true"


def test_params():
    url = URL(url_full, params={"pp": "hello"})
    assert url.path == "/subdir/file.py"
    assert url.host == "gevent.org"
    assert url.port == 80
    assert url.query == "param=value&other=true&pp=hello"
    assert url.fragment == "frag"


def test_params_urlencoded():
    url = URL(url_full, params={"a/b": "c/d"})
    assert url.path == "/subdir/file.py"
    assert url.host == "gevent.org"
    assert url.port == 80
    assert url.query == "param=value&other=true&a%2Fb=c%2Fd"
    assert url.fragment == "frag"


def test_query_urlencoded():
    url = URL("http://gevent.org/?foo=bar with spaces")
    assert url.query == "foo=bar%20with%20spaces"
    assert url.host == "gevent.org"
    assert url.port == 80


def test_tuple_unpack():
    url = URL("http://gevent.org/somepath?foo=bar#frag")
    assert len(tuple(url)) == 6
    scheme, netloc, path, params, query, fragment = url
    assert scheme == "http"
    assert netloc == "gevent.org"
    assert path == "/somepath"
    assert query == "foo=bar"
    assert fragment == "frag"


def test_tuple_unpack_no_none():
    url = URL("http://gevent.org/")
    assert len(tuple(url)) == 6
    assert not any(val is None for val in tuple(url))


def test_empty():
    url = URL()
    assert url.host == ""
    assert not url.port
    assert url.query == ""
    assert url.fragment == ""
    assert url.netloc == ""
    assert str(url) == ""


def test_empty_path():
    assert URL("http://gevent.org").path == ""


def test_consistent_reparsing():
    for surl in (url_full, url_path_only):
        url = URL(surl)
        reparsed = URL(str(url))
        for attr in URL.__slots__:
            assert getattr(reparsed, attr) == getattr(url, attr)


def test_redirection_abs_path():
    url = URL(url_full)
    updated = url.redirect("/test.html")
    assert updated.host == url.host
    assert updated.port == url.port
    assert updated.path == "/test.html"
    assert updated.query == ""
    assert updated.fragment == ""


@pytest.mark.parametrize("redirection", ("test.html?key=val", "folder/test.html?key=val"))
def test_redirection_rel_path(redirection):
    url = URL(url_full)
    updated = url.redirect(redirection)
    assert updated.host == url.host
    assert updated.port == url.port
    assert updated.path.startswith("/subdir/")
    assert updated.path.endswith(redirection.split("?", 1)[0])
    assert updated.query == "key=val"
    assert updated.fragment == ""


def test_redirection_full_path():
    url_full2_plain = "http://google.de/index"
    url = URL(url_full)
    updated = url.redirect(url_full2_plain)
    url_full2 = URL(url_full2_plain)
    for attr in URL.__slots__:
        assert getattr(updated, attr) == getattr(url_full2, attr)
    assert str(url_full2) == url_full2_plain


def test_query():
    assert URL("/some/url", params={"a": "b", "c": 2}).query == "a=b&c=2"


def test_equality():
    assert URL("https://example.com/") != URL("http://example.com/")
    assert URL("http://example.com/") == URL("http://example.com/")


def test_default_port():
    assert URL("https://python.org").port == 443
    assert URL("http://gevent.org").port == 80
    assert URL("example.com").port is None


def test_pw():
    url = URL("http://asdf:dd@example.com/index.php?aaaa=bbbbb")
    assert url.host == "example.com"
    assert url.port == 80
    assert url.user == "asdf"
    assert url.password == "dd"


def test_pw_with_port():
    url = URL("http://asdf:dd@example.com:90/index.php?aaaa=bbbbb")
    assert url.host == "example.com"
    assert url.port == 90
    assert url.user == "asdf"
    assert url.password == "dd"


def test_ipv6():
    url = URL("http://[2001:db8:85a3:8d3:1319:8a2e:370:7348]/")
    assert url.host == "2001:db8:85a3:8d3:1319:8a2e:370:7348"
    assert url.port == 80
    assert url.user == ""


def test_ipv6_with_port():
    url = URL("https://[2001:db8:85a3:8d3:1319:8a2e:370:7348]:8080/")
    assert url.host == "2001:db8:85a3:8d3:1319:8a2e:370:7348"
    assert url.port == 8080
    assert url.user == ""
