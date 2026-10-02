"""Tests for the shared auth helpers and the requests Session(auth=) surface."""

import base64

import pytest

from geventhttpclient import BasicAuth
from geventhttpclient.auth import resolve_auth


def test_basicauth_token_is_basic_with_base64_of_user_colon_pass():
    token = BasicAuth("user", "pass").auth_header()
    assert token == "Basic " + base64.b64encode(b"user:pass").decode("ascii")


def test_basicauth_uses_latin1_for_ascii_only_credentials():
    auth = BasicAuth("user", "pass")
    assert auth.encoding == "iso-8859-1"
    header = auth.auth_header()
    assert header == "Basic " + base64.b64encode(b"user:pass").decode("ascii")


def test_basicauth_falls_back_to_utf8_for_non_latin1_codepoints():
    # RFC 7617: credentials with a code point that latin-1 cannot represent
    # (U+00E9 "é" is in range and works in latin-1; U+20AC "€" is not).
    auth = BasicAuth("üser", "pa€")
    assert auth.encoding == "utf-8"
    decoded = base64.b64decode(auth.auth_header().removeprefix("Basic "))
    assert decoded == "üser:pa€".encode()


def test_basicauth_explicit_encoding_skips_autodetect():
    # An explicit ``encoding=...`` is a single shot. latin-1 cannot
    # represent ``€`` (U+20AC), so a strict encode raises - the caller
    # asked for that codec and gets a clear error rather than a
    # silent substitution.
    with pytest.raises(UnicodeEncodeError):
        BasicAuth("üser", "pa€", encoding="iso-8859-1")


def test_basicauth_explicit_encoding_strict_raises():
    # An unknown codec raises on encode so the caller can see the mistake.
    with pytest.raises(LookupError):
        BasicAuth("user", "pass", encoding="does-not-exist")


def test_basicauth_explicit_utf8_encoding():
    auth = BasicAuth("user", "päss", encoding="utf-8")
    assert auth.encoding == "utf-8"
    decoded = base64.b64decode(auth.auth_header().removeprefix("Basic "))
    assert decoded == "user:päss".encode()


def test_basicauth_username_and_password_accessors():
    auth = BasicAuth("user", "pass")
    assert auth.username == "user"
    assert auth.password == "pass"


def test_basicauth_repr_does_not_leak_the_password():
    assert "pass" not in repr(BasicAuth("user", "pass"))


def test_resolve_auth_none_returns_none():
    assert resolve_auth(None) is None


def test_resolve_auth_basicauth_object_returns_header():
    auth = BasicAuth("user", "pass")
    assert resolve_auth(auth) == auth.auth_header()


def test_resolve_auth_tuple_uses_basicauth_encoding():
    assert resolve_auth(("user", "pass")) == BasicAuth("user", "pass").auth_header()


def test_resolve_auth_passes_through_strings():
    assert resolve_auth("Bearer xyz") == "Bearer xyz"


def test_resolve_auth_invalid_raises():
    # The values below intentionally violate the AuthValue type; we
    # exercise the runtime NotImplementedError path.
    for bad in (123, ("a", "b", "c"), object()):
        with pytest.raises(NotImplementedError):
            resolve_auth(bad)  # type: ignore[arg-type]


def test_top_level_import():
    assert BasicAuth.__module__ == "geventhttpclient.auth"
    from geventhttpclient import BasicAuth as Exported

    assert Exported is BasicAuth
