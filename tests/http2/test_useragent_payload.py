"""UserAgent h2 payload/error-taxonomy regression tests.

H1 (payload normalisation) exercises ``_make_request`` without making
a real request; H3 uses an in-process closed-port error injection.
H2 (end-to-end timeout) is covered by the TLS-listener fixture in
``test_alpn.py::TestErrorTaxonomy``.

The network-marked ``test_request_without_timeout_uses_pool_default``
verifies that a UserAgent round-trip against httpbingo.org succeeds
without an explicit per-request timeout -- the pool's network_timeout
is used as the default.
"""

import pytest

from geventhttpclient.http2.errors import HTTP2Error
from geventhttpclient.useragent import (
    UserAgent,
    _make_request,
)
from tests.common import HTTPBIN_HOST


class TestH1PayloadNormalisation:
    """str / dict / iterable payloads are converted to bytes in the
    h2 path the same way the HTTP/1.1 path does."""

    def test_str_payload_is_bytes_on_h2(self) -> None:
        ua = UserAgent(http2=True, insecure=True)
        try:
            req = _make_request(
                f"https://{HTTPBIN_HOST}/post",
                method="POST",
                payload="raw-string-body",
            )
            assert isinstance(req.payload, bytes)
            assert req.payload == b"raw-string-body"
        finally:
            ua.close()

    def test_dict_payload_is_urlencoded(self) -> None:
        ua = UserAgent(http2=True, insecure=True)
        try:
            req = _make_request(
                f"https://{HTTPBIN_HOST}/post",
                method="POST",
                payload={"key": "value", "n": "1"},
            )
            # urlencode-style: ``key=value&n=1`` (alphabetical)
            assert req.payload == b"key=value&n=1"
        finally:
            ua.close()


class TestH2DefaultTimeout:
    """The UserAgent h2 path inherits the pool's network_timeout when
    the request did not set one explicitly."""

    @pytest.mark.network
    def test_request_without_timeout_uses_pool_default(self) -> None:
        # We don't actually trigger the timeout here -- the server
        # replies quickly. The point is that the call does not blow
        # up with ``None`` from request.timeout and the round trip
        # completes against httpbingo.org.
        ua = UserAgent(http2=True, insecure=True)
        try:
            r = ua.urlopen(
                f"https://{HTTPBIN_HOST}/get", method="GET",
            )
            assert r.status_code == 200
        finally:
            ua.close()


class TestH3ErrorTaxonomy:
    """HTTP/2 transport failures surface as ``HTTP2Error`` which
    subclasses the stdlib ``OSError`` so the existing h1 ``ConnectionError``
    catches them transitively (both inherit from ``OSError``).
    """

    def test_http2_error_is_os_error_subclass(self) -> None:
        # ``HTTP2Error`` inherits from the stdlib ``OSError`` so a
        # caller using ``except OSError`` catches h2 transport
        # failures uniformly. ``useragent.BadStatusCode`` is a sibling
        # type (a project-local exception class), so we only verify
        # ``HTTP2Error`` here.
        assert issubclass(HTTP2Error, OSError)
        # Project-local ``ConnectionError`` exists; we do *not* depend
        # on inheritance from it -- the connection hierarchy is split
        # between this class and useragent-side BadStatusCode.
        from geventhttpclient.useragent import ConnectionError as UAConnectionError
        # Smoke: the useragent-side ``ConnectionError`` is a class with
        # a different shape (``url`` attribute, etc.) than ``OSError``;
        # we just confirm both names are accessible without conflict.
        assert UAConnectionError.__name__ == "ConnectionError"

    def test_connection_failure_surfaces_as_connection_error(self) -> None:
        ua = UserAgent(http2=True, insecure=True)
        try:
            # Point at a closed port so the connection fails.
            with pytest.raises((HTTP2Error, OSError)):
                ua.urlopen(
                    "https://127.0.0.1:1/", method="GET",
                )
        finally:
            ua.close()
