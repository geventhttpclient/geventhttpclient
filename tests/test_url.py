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


def test_quote_spaces():
    url = URL("http://gevent.org/?foo=bar with spaces")
    assert url.quoted == "http://gevent.org/?foo=bar%20with%20spaces"
    assert url.quoted_uri == "/?foo=bar%20with%20spaces"
    assert url.host == "gevent.org"
    assert url.port == 80


def test_quote_non_ascii():
    url = URL("http://127.0.0.1:8000/ы")
    assert url.quoted == "http://127.0.0.1:8000/%D1%8B"
    assert url.quoted_uri == "/%D1%8B"
    assert url.host == "127.0.0.1"
    assert url.port == 8000


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


@pytest.mark.parametrize(
    ("redirection", "expected_path"),
    [
        ("test.html?key=val", "/subdir/test.html"),
        ("folder/test.html?key=val", "/subdir/folder/test.html"),
    ],
)
def test_redirection_rel_path(redirection, expected_path):
    """RFC 3986 section 5.3: a relative path merges against all but the last
    segment of the base path, not against the full base path."""
    url = URL(url_full)
    updated = url.redirect(redirection)
    assert updated.host == url.host
    assert updated.port == url.port
    assert updated.path == expected_path
    assert updated.query == "key=val"
    assert updated.fragment == ""


@pytest.mark.parametrize(
    ("redirection", "expected"),
    [
        # RFC 3986 section 5.4.2, normal examples (base http://a/b/c/d?q)
        ("g", "http://a/b/c/g"),
        ("./g", "http://a/b/c/g"),
        ("g/", "http://a/b/c/g/"),
        ("/g", "http://a/g"),
        ("//g", "http://g"),
        ("?y", "http://a/b/c/d?y"),
        ("g?y", "http://a/b/c/g?y"),
        ("#s", "http://a/b/c/d?q#s"),
        ("g#s", "http://a/b/c/g#s"),
        ("g?y#s", "http://a/b/c/g?y#s"),
        # RFC 3986 section 5.4.2, abnormal examples
        ("../../../g", "http://a/g"),
        ("../../../../g", "http://a/g"),
        ("/./g", "http://a/g"),
        ("/../g", "http://a/g"),
        ("g.", "http://a/b/c/g."),
        (".g", "http://a/b/c/.g"),
        ("./../g", "http://a/b/g"),
        ("./g/.", "http://a/b/c/g/"),
        ("g/./h", "http://a/b/c/g/h"),
        ("g/../h", "http://a/b/c/h"),
        ("../up", "http://a/b/up"),
        ("/a/b/c/../up", "http://a/a/b/up"),
        # query and fragment content never takes part in resolution
        ("g?y/./x", "http://a/b/c/g?y/./x"),
        ("g#s/../x", "http://a/b/c/g#s/../x"),
    ],
)
def test_redirection_resolution_rfc3986_5_4(redirection, expected):
    assert URL("http://a/b/c/d?q").redirect(redirection) == URL(expected)


def test_redirection_params_only_reference_merges_as_a_path_segment():
    """A reference like ``;x`` has an empty path with parameters; it merges
    as a path segment of its own, not as parameters of the base path, and
    the base parameters do not leak into it (urljoin behaves the same)."""
    url = URL("https://example.com/a/b/c/d;p?q")
    updated = url.redirect(";x")
    assert str(updated) == "https://example.com/a/b/c/;x"


def test_redirection_absolute_reference_drops_dot_segments():
    """Dot segment removal applies to absolute paths and full URLs too
    (RFC 3986 section 5.2.2 runs remove_dot_segments on every resolved
    path)."""
    url = URL("https://example.com/dir/page")
    assert url.redirect("/a/../b").path == "/b"
    updated = url.redirect("https://other.example.com/x/../y")
    assert str(updated) == "https://other.example.com/y"


def test_redirection_protocol_relative_keeps_base_scheme():
    """``//host/path`` resolves against the base URI (RFC 3986 section 5.2.2):
    the scheme must survive so an https connection is not downgraded to plain
    http by a redirect."""
    url = URL("https://example.com/dir/page")
    updated = url.redirect("//other.example.com/p")
    assert updated.scheme == "https"
    assert updated.host == "other.example.com"
    assert updated.port == 443
    assert updated.path == "/p"
    assert str(updated) == "https://other.example.com/p"


def test_redirection_with_explicit_scheme_is_untouched():
    url = URL("https://example.com/dir/page")
    updated = url.redirect("http://other.example.com/p")
    url_full2 = URL("http://other.example.com/p")
    for attr in URL.__slots__:
        assert getattr(updated, attr) == getattr(url_full2, attr)


def test_redirection_query_only_keeps_base_path():
    """A reference with an empty path keeps the base path (RFC 3986 section
    5.2.2); the merge must not turn /a/b into /a/b/."""
    url = URL("https://example.com/a/b?old=1")
    updated = url.redirect("?new=2")
    assert updated.path == "/a/b"
    assert updated.query == "new=2"
    assert updated.fragment == ""


def test_redirection_query_only_keeps_base_path_parameters():
    """urlparse splits ``;p`` out of the last path segment into its own
    field; RFC 3986 section 5.2.2 keeps the whole base path for references
    with an empty path - parameters included, like urljoin does."""
    url = URL("https://example.com/a/b/c/d;p?q")
    updated = url.redirect("?new=2")
    assert updated.path == "/a/b/c/d"
    assert updated.params == "p"
    assert updated.query == "new=2"
    assert str(updated) == "https://example.com/a/b/c/d;p?new=2"


def test_redirection_fragment_only_keeps_path_and_query():
    """Without a reference path or query, the base path and base query both
    survive; only the fragment is replaced."""
    url = URL("https://example.com/a/b?old=1")
    updated = url.redirect("#frag")
    assert updated.path == "/a/b"
    assert updated.query == "old=1"
    assert updated.fragment == "frag"


def test_redirection_empty_reference_resolves_to_base_without_fragment():
    url = URL("https://example.com/a/b?old=1")
    updated = url.redirect("")
    assert updated == URL("https://example.com/a/b?old=1")


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


def test_absent_parts_are_empty_strings():
    """Parts the URL does not carry stay '', they are never None."""
    url = URL("http://gevent.org/path")
    assert url.user == ""
    assert url.username == ""
    assert url.password == ""
    assert url.params == ""
    assert url.query == ""
    assert url.fragment == ""


def test_userinfo_and_lowercased_host():
    url = URL("http://USER:pw@Gevent.ORG/path")
    assert url.user == "USER"
    assert url.username == "USER"
    assert url.password == "pw"
    assert url.host == "gevent.org"
    assert url.hostname == "gevent.org"


def test_unknown_part_raises_attribute_error():
    url = URL(url_full)
    assert url.geturl() == url_full
    with pytest.raises(AttributeError):
        _ = url.nonexistent_part
