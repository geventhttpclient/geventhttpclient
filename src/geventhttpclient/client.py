import base64
import errno
import os
import re
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import IO, Any, Union

import gevent
import gevent.socket

from geventhttpclient import __version__

# Review_http2_3.md H3: h2 transport failures are wrapped in a
# subclass of ``ConnectionError`` so the UserAgent retry loop and
# locust-style callers can catch them uniformly. The class lives in
# ``useragent.py`` to avoid a circular import; we re-export it under
# the same name for documentation.
from geventhttpclient._http2_errors import HTTP2Error
from geventhttpclient.connectionpool import ConnectionPool, SSLConnectionPool
from geventhttpclient.header import Headers, HeadersDataType
from geventhttpclient.http2_pool import HTTP2ConnectionPool, HTTP2ConnectionPoolError
from geventhttpclient.http2_session import HTTP2ResponseHandle, HTTP2Session
from geventhttpclient.response import (
    HTTPConnectionClosed,
    HTTPParseError,
    HTTPResponse,
    HTTPSocketPoolResponse,
)
from geventhttpclient.url import URL

# RFC 9110 section 9.1 and section 5.1: a method and a header field name
# are tokens. Section 5.5: a field value carries no CR and no LF - request
# smuggling builds on exactly those bytes (RFC 9112 section 5.2 deprecates
# folding). Section 3: a request target carries no whitespace and no
# control characters.
_TOKEN_RE = re.compile(r"\A[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_FIELD_VALUE_RE = re.compile(r"\A[\t\x20-\x7e\x80-\xff]*\Z")
_REQUEST_TARGET_RE = re.compile(r"\A[^\x00-\x20\x7f]*\Z")

# RFC 9110 section 9.2.1: GET, HEAD, OPTIONS, TRACE, PUT and DELETE are the
# idempotent methods.
IDEMPOTENT_METHODS = frozenset(("GET", "HEAD", "OPTIONS", "TRACE", "PUT", "DELETE"))


# RFC 9110 sections 9.3.1 and 9.3.3 and RFC 5789: the methods whose
# requests define a meaning for enclosed content.
_BODY_CARRYING_METHODS = frozenset(("POST", "PUT", "PATCH"))


def _may_retry_after_send_error(method: str) -> bool:
    """RFC 9110 section 9.2.2: a client SHOULD NOT automatically retry a
    request with a non-idempotent method once it may have been processed.
    After a send error part or all of the body may have reached the server,
    so the retry is limited to idempotent methods."""
    return method.upper() in IDEMPOTENT_METHODS


CRLF = "\r\n"
WHITESPACE = " "
FIELD_VALUE_SEP = ": "
HOST_PORT_SEP = ":"
SLASH = "/"
PROTO_HTTP = "http"
PROTO_HTTPS = "https"
HEADER_HOST = "Host"
HEADER_CONTENT_LENGTH = "Content-Length"
HEADER_TRANSFER_ENCODING = "Transfer-Encoding"
TRANSFER_ENCODING_CHUNKED = "chunked"
HEADER_PROXY_AUTHORIZATION = "Proxy-Authorization"
HEADER_EXPECT = "Expect"
EXPECT_100_CONTINUE = "100-continue"

METHOD_GET = "GET"
METHOD_HEAD = "HEAD"
METHOD_POST = "POST"
METHOD_PUT = "PUT"
METHOD_DELETE = "DELETE"
METHOD_PATCH = "PATCH"
METHOD_OPTIONS = "OPTIONS"
METHOD_TRACE = "TRACE"


def _get_body_length(body: Any) -> int | None:
    """
    Get len of string or file

    :param body:
    :return:
    :rtype: int
    """
    try:
        return len(body)
    except TypeError:
        try:
            return os.fstat(body.fileno()).st_size
        except (AttributeError, OSError):
            return None


def _uses_chunked_transfer(
    header_fields: HeadersDataType,
    body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None,
) -> bool:
    """Check whether the body must be sent with chunked transfer coding.

    That is the case when the user requested `Transfer-Encoding: chunked`, or
    when the body is a stream of unknown length (iterable or file-like object
    without usable fileno) that cannot be announced with a Content-Length.
    """
    for field, value in header_fields.items():
        if (
            field.lower() == HEADER_TRANSFER_ENCODING.lower()
            and TRANSFER_ENCODING_CHUNKED in str(value).lower()
        ):
            return True
    if body is None:
        # No body at all; unknown-length streams are chunked, None is not.
        return False
    return not isinstance(body, (bytes, bytearray, memoryview)) and _get_body_length(body) is None


def _requests_100_continue(header_fields: HeadersDataType) -> bool:
    """Check whether the merged headers request `Expect: 100-continue`."""
    for field, value in header_fields.items():
        if field.lower() == HEADER_EXPECT.lower() and EXPECT_100_CONTINUE in str(value).lower():
            return True
    return False


def _iter_chunked(body: Any, block_size: int) -> Iterator[bytes]:
    """Encode the given body with chunked transfer coding (RFC 9112, section 7.1).

    Accepts bytes-like data, a file-like object with `read` or any iterable
    of bytes/str blocks and yields ready-to-send encoded blocks, terminated
    by the final zero-size chunk.
    """
    if isinstance(body, (bytes, bytearray, memoryview)):
        data = memoryview(body)
        for offset in range(0, len(data), block_size):
            block: memoryview = data[offset : offset + block_size]
            yield b"%x\r\n" % len(block) + bytes(block) + b"\r\n"
    elif hasattr(body, "read"):
        while True:
            block = body.read(block_size)
            if not block:
                break
            if isinstance(block, str):
                block = block.encode("utf-8")
            yield b"%x\r\n" % len(block) + block + b"\r\n"
    else:
        for block in body:
            if isinstance(block, str):
                block = block.encode("utf-8")
            elif not isinstance(block, (bytes, bytearray, memoryview)):
                raise TypeError(
                    "chunked body items must be bytes or str, not " + type(block).__name__
                )
            if block:
                yield b"%x\r\n" % len(block) + bytes(block) + b"\r\n"
    yield b"0\r\n\r\n"


class _ExpectContinueProbe(HTTPResponse):
    """Parser tracking whether an interim 1xx message has been completed."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.interim_seen = False

    def _on_message_complete(self) -> None:
        if self.get_code() < 200:
            self.interim_seen = True
        super()._on_message_complete()


class HTTPClient:
    HTTP_11 = "HTTP/1.1"
    HTTP_10 = "HTTP/1.0"

    BLOCK_SIZE = 1024 * 4  # 4KB

    DEFAULT_HEADERS = Headers({"User-Agent": "python/gevent-http-client-" + __version__})

    @classmethod
    def from_url(cls, url: str | URL, **kw: Any) -> "HTTPClient":
        if not isinstance(url, URL):
            url = URL(url)
        enable_ssl = url.scheme == PROTO_HTTPS
        if not enable_ssl:
            kw.pop("ssl_options", None)
        return cls(url.host, port=url.port, ssl=enable_ssl, **kw)

    def __init__(
        self,
        host: str,
        port: int | None = None,
        headers: HeadersDataType | None = None,
        block_size: int = BLOCK_SIZE,
        connection_timeout: float = ConnectionPool.DEFAULT_CONNECTION_TIMEOUT,
        network_timeout: float = ConnectionPool.DEFAULT_NETWORK_TIMEOUT,
        disable_ipv6: bool = False,
        concurrency: int = 1,
        ssl: bool = False,
        ssl_options: dict[str, Any] | None = None,
        ssl_context_factory: Callable[..., gevent.ssl.SSLContext] | None = None,
        insecure: bool = False,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        proxy_user: str | None = None,
        proxy_password: str | None = None,
        version: str = HTTP_11,
        headers_type: type[Headers] = Headers,
        http2: bool = False,
    ) -> None:
        if headers is None:
            headers = headers_type()
        self.host = host
        self.port = port
        connection_host = self.host
        connection_port = self.port
        if proxy_host is not None:
            assert proxy_port is not None, "you have to provide proxy_port if you set proxy_host"
            self.use_proxy = True
            connection_host = proxy_host
            connection_port = proxy_port
            self._proxy_credentials = None
            if proxy_user is not None or proxy_password is not None:
                self._proxy_credentials = f"{proxy_user or ''}:{proxy_password or ''}"
        else:
            self.use_proxy = False
            self._proxy_credentials = None
        if ssl:
            ssl_options = ssl_options.copy() if ssl_options else {}
        if ssl_options is not None:
            if ssl_context_factory is not None:
                requested_hostname = headers.get("host", self.host)
                ssl_options.setdefault("server_hostname", requested_hostname)
            self.ssl = True
            if not self.port:
                self.port = 443
            if not connection_port:
                connection_port = self.port
            self._connection_pool: ConnectionPool = SSLConnectionPool(
                connection_host,
                connection_port,
                self.host,
                self.port,
                size=concurrency,
                ssl_options=ssl_options,
                ssl_context_factory=ssl_context_factory,
                insecure=insecure,
                network_timeout=network_timeout,
                connection_timeout=connection_timeout,
                disable_ipv6=disable_ipv6,
                use_proxy=self.use_proxy,
                proxy_user=proxy_user,
                proxy_password=proxy_password,
            )
        else:
            self.ssl = False
            if not self.port:
                self.port = 80
            if not connection_port:
                connection_port = self.port
            self._connection_pool = ConnectionPool(
                connection_host,
                connection_port,
                self.host,
                self.port,
                size=concurrency,
                network_timeout=network_timeout,
                connection_timeout=connection_timeout,
                disable_ipv6=disable_ipv6,
                use_proxy=self.use_proxy,
                proxy_user=proxy_user,
                proxy_password=proxy_password,
            )
        self.version = version
        self.headers_type = headers_type
        self.default_headers = headers_type()
        self.default_headers.update(self.DEFAULT_HEADERS)
        self.default_headers.update(headers)
        self.block_size = block_size

        scheme = PROTO_HTTPS if self.ssl else PROTO_HTTP
        port_str = f":{port}" if port else ""
        self._base_url_string = f"{scheme}://{self.host}{port_str}/"

        # HTTP/2 pool (Sprint 3b). Created only when explicitly enabled
        # and the target uses TLS — h2 over plaintext (h2c) needs a
        # separate code path (prior-knowledge mode) and is out of Sprint
        # 3b scope.
        # httpx-style opt-in switch (``http2=True``); the default
        # ``False`` keeps the HTTP/1.1 path untouched.
        self.http2 = http2
        self._h2_pool: HTTP2ConnectionPool | None = (
            HTTP2ConnectionPool(
                connection_timeout=connection_timeout,
                network_timeout=network_timeout,
                insecure=insecure,
            )
            if http2 and self.ssl
            else None
        )

    def close(self) -> None:
        if self._h2_pool is not None:
            self._h2_pool.close()
        self._connection_pool.close()

    # a body without a usable len() falls back to the size of its file in
    # `_get_body_length`, and to chunked transfer when even that is unknown

    def _build_request(
        self,
        method: str,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = b"",
        headers: HeadersDataType | None = None,
        chunked: bool | None = None,
    ) -> str:
        """

        :param method:
        :type method: str or bytes
        :param request_uri:
        :type request_uri: str or bytes
        :param body:
        :type body: str or bytes or file or iterable
        :param headers:
        :type headers: dict
        :param chunked:
            Precomputed chunked-transfer decision from `request`. When None,
            it is derived from the merged headers and the body type.
        :return:
        :rtype: str or bytes
        """

        if headers is None:
            headers = {}

        if not isinstance(method, str) or not _TOKEN_RE.fullmatch(method):
            raise ValueError(f"invalid HTTP method {method!r}")
        if not isinstance(request_uri, str) or not _REQUEST_TARGET_RE.fullmatch(request_uri):
            raise ValueError(f"invalid request URI {request_uri!r}")
        if request_uri.startswith("//"):
            # origin-form has exactly one leading slash (RFC 9112 section
            # 3.2); "//host/path" is a protocol-relative reference that would
            # be sent as a literal path with an empty first segment
            raise ValueError(
                f"invalid request URI {request_uri!r}: protocol-relative targets are not origin-form"
            )

        header_fields = self.headers_type()
        header_fields.update(self.default_headers)
        header_fields.update(headers)
        if self.version == self.HTTP_11 and HEADER_HOST not in header_fields:
            host_port = self.host
            # IPv6 addresses require square brackets in the Host header.
            if ":" in self.host and self.host[0] != "[" and self.host[-1] != "]":
                host_port = "[" + host_port + "]"
            if self.port not in (80, 443):
                host_port += HOST_PORT_SEP + str(self.port)
            header_fields[HEADER_HOST] = host_port
        if (
            self.use_proxy
            and not self.ssl
            and self._proxy_credentials is not None
            and HEADER_PROXY_AUTHORIZATION not in header_fields
        ):
            # the plain HTTP proxy is the recipient of the request and needs
            # the credentials; inside a CONNECT tunnel they must not be sent
            token = base64.b64encode(self._proxy_credentials.encode("utf-8")).decode("ascii")
            header_fields[HEADER_PROXY_AUTHORIZATION] = f"Basic {token}"
        if chunked is None:
            chunked = _uses_chunked_transfer(header_fields, body)
        if chunked and HEADER_TRANSFER_ENCODING not in header_fields:
            header_fields[HEADER_TRANSFER_ENCODING] = TRANSFER_ENCODING_CHUNKED
        if chunked and HEADER_CONTENT_LENGTH in header_fields:
            # Content-Length must not be sent alongside Transfer-Encoding: chunked
            del header_fields[HEADER_CONTENT_LENGTH]
        if body and not chunked and HEADER_CONTENT_LENGTH not in header_fields:
            body_length = _get_body_length(body)
            if body_length:
                header_fields[HEADER_CONTENT_LENGTH] = str(body_length)
        elif not chunked and HEADER_CONTENT_LENGTH not in header_fields:
            # RFC 9112 section 6.3: methods that define a meaning for
            # enclosed content SHOULD carry Content-Length. An empty or
            # absent body is a zero, like curl and http.client send it.
            if method.upper() in _BODY_CARRYING_METHODS:
                header_fields[HEADER_CONTENT_LENGTH] = "0"

        request_url = request_uri
        if self.use_proxy and not self.ssl:
            # A plain HTTP proxy is the recipient of the request and requires
            # the absolute request URI. With CONNECT tunneling (SSL targets)
            # the tunnel is transparent and the origin form must be used.
            base_url = self._base_url_string
            if request_uri.startswith(SLASH):
                base_url = base_url[:-1]
            request_url = base_url + request_url
        elif not request_url.startswith((SLASH, PROTO_HTTP)):
            request_url = SLASH + request_url
        elif request_url.startswith(PROTO_HTTP):
            if request_url.startswith(self._base_url_string):
                request_url = request_url[len(self._base_url_string) - 1 :]
            else:
                raise ValueError("Invalid host in URL")

        request = method + WHITESPACE + request_url + WHITESPACE + self.version + CRLF

        for field, value in header_fields.items():
            if not isinstance(field, str) or not _TOKEN_RE.fullmatch(field):
                raise ValueError(f"invalid header field name {field!r}")
            if isinstance(value, str) and not _FIELD_VALUE_RE.fullmatch(value):
                raise ValueError(f"invalid value for header field {field!r}: {value!r}")
            request += field + FIELD_VALUE_SEP + str(value) + CRLF
        request += CRLF
        return request

    def request(
        self,
        method: str,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = b"",
        headers: HeadersDataType | None = None,
    ) -> HTTPSocketPoolResponse:
        """

        :param method:
        :param request_uri:
        :param body: byte or file
        :param headers:
        :return:
        """

        if body is None:
            # None means "no body", not a body of unknown length.
            body = b""
        elif isinstance(body, str):
            body = body.encode("utf-8")

        # the same case-insensitive merge `_build_request` performs before
        # deciding on the Content-Length header
        merged_headers = self.headers_type()
        merged_headers.update(self.default_headers)
        merged_headers.update(headers or {})
        chunked = _uses_chunked_transfer(merged_headers, body)
        if chunked and self.version != self.HTTP_11:
            raise ValueError("Chunked transfer encoding requires HTTP/1.1")

        # `Expect: 100-continue` is an HTTP/1.1 mechanism; with earlier
        # versions the header is sent as-is and the body sent right away.
        expect_continue = self.version == self.HTTP_11 and _requests_100_continue(merged_headers)

        request = self._build_request(
            method.upper(), request_uri, body=body, headers=headers, chunked=chunked
        )

        attempts_left = self._connection_pool.size + 1

        while True:
            sock = self._connection_pool.get_socket()
            try:
                # the request head speaks latin-1, like the response header
                # decoding and http.client; characters outside latin-1 fail
                # loudly instead of leaving UTF-8 mojibake on the wire
                _request = request.encode("latin-1")
                if expect_continue:
                    sock.sendall(_request)
                    try:
                        remainder, final_data = self._wait_for_continue_response(sock, method)
                    except HTTPConnectionClosed:
                        # no response object was created, release the socket
                        self._connection_pool.release_socket(sock)
                        if attempts_left > 0:
                            attempts_left -= 1
                            continue
                        raise
                    if final_data is not None:
                        # the server rejected the request without an interim
                        # response, return it and do not send the body
                        try:
                            response = HTTPSocketPoolResponse(
                                sock,
                                self._connection_pool,
                                block_size=self.block_size,
                                method=method.upper(),
                                headers_type=self.headers_type,
                                pre_buffered=final_data,
                            )
                        except HTTPConnectionClosed:
                            # connection is released by the response itself
                            if attempts_left > 0 and _may_retry_after_send_error(method):
                                attempts_left -= 1
                                continue
                            raise
                        response._sent_request = request
                        return response
                    self._send_body_after_continue(sock, body, chunked)
                    assert remainder is not None
                    try:
                        response = HTTPSocketPoolResponse(
                            sock,
                            self._connection_pool,
                            block_size=self.block_size,
                            method=method.upper(),
                            headers_type=self.headers_type,
                            pre_buffered=remainder,
                        )
                    except HTTPConnectionClosed:
                        # connection is released by the response itself
                        if attempts_left > 0 and _may_retry_after_send_error(method):
                            attempts_left -= 1
                            continue
                        raise
                    response._sent_request = request
                    return response
                if chunked:
                    # Note: on retry, file-like/iterable bodies continue
                    # from their current position or are exhausted, same
                    # as with `sendfile` before.
                    if body:
                        sock.sendall(_request)
                        for block in _iter_chunked(body, self.block_size):
                            sock.sendall(block)
                    else:
                        # Single write: a separate small write stalls on Nagle.
                        sock.sendall(_request + b"0\r\n\r\n")
                elif body:
                    if isinstance(body, bytes):
                        sock.sendall(_request + body)
                    else:
                        sock.sendall(_request)
                        sock.sendfile(body)
                else:
                    sock.sendall(_request)
            except gevent.socket.error as e:
                if e.errno not in (errno.ECONNRESET, errno.EPIPE):
                    self._connection_pool.release_socket(sock)
                    raise
                # The connection broke while the request was being sent. The
                # server may have rejected the request early (e.g. 401/403 on
                # a huge body) and already sent its response before closing
                # the connection. Try to retrieve that response instead of
                # hiding it behind the socket error. The response releases the
                # socket itself in both the success and the error case.
                try:
                    response = HTTPSocketPoolResponse(
                        sock,
                        self._connection_pool,
                        block_size=self.block_size,
                        method=method.upper(),
                        headers_type=self.headers_type,
                    )
                except (gevent.socket.error, HTTPParseError):
                    # no pending (valid) response, socket is released already
                    if attempts_left > 0 and _may_retry_after_send_error(method):
                        attempts_left -= 1
                        continue
                    raise
                else:
                    response._sent_request = request
                    return response

            try:
                response = HTTPSocketPoolResponse(
                    sock,
                    self._connection_pool,
                    block_size=self.block_size,
                    method=method.upper(),
                    headers_type=self.headers_type,
                )
            except HTTPConnectionClosed:
                # connection is released by the response itself; the request
                # was sent in full, so the server may have processed it and
                # the same RFC 9112 section 9.2.2 limit applies
                if attempts_left > 0 and _may_retry_after_send_error(method):
                    attempts_left -= 1
                    continue
                raise
            else:
                response._sent_request = request
                return response

    def _send_body_after_continue(
        self,
        sock: gevent.socket.socket,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes],
        chunked: bool,
    ) -> None:
        """Send the request body after an interim 100 Continue response."""
        if chunked:
            if body:
                for block in _iter_chunked(body, self.block_size):
                    sock.sendall(block)
            else:
                sock.sendall(b"0\r\n\r\n")
        elif isinstance(body, bytes):
            sock.sendall(body)
        elif body:
            sock.sendfile(body)

    def _wait_for_continue_response(
        self, sock: gevent.socket.socket, method: str
    ) -> tuple[bytes | None, bytes | None]:
        """Wait for the answer to a request with `Expect: 100-continue`.

        Returns a tuple `(remainder, final_data)`. When the server answered
        with an interim 100 Continue response, `final_data` is None and
        `remainder` holds already received bytes of the final response (may
        be empty). When the server sent a final response instead, `final_data`
        holds the received bytes of that response and the request body must
        not be sent.
        """
        probe = _ExpectContinueProbe(method=method.upper(), headers_type=self.headers_type)
        data = b""
        while True:
            if probe.interim_seen:
                # the interim response invited us to continue, send the body
                # even when the final response has already been received;
                # everything beyond the interim header terminator belongs to
                # the final response
                return data.split(b"\r\n\r\n", 1)[1], None
            if probe.headers_complete and probe.get_code() >= 200:
                return None, data
            block = sock.recv(self.block_size)
            if not block:
                raise HTTPConnectionClosed(
                    "connection closed while waiting for 100 Continue response"
                )
            probe.feed(block)
            data += block

    def get(
        self, request_uri: str, headers: Mapping[str, Any] | None = None
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_GET, request_uri, headers=headers)

    def head(
        self, request_uri: str, headers: Mapping[str, Any] | None = None
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_HEAD, request_uri, headers=headers)

    def post(
        self,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = "",
        headers: HeadersDataType | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_POST, request_uri, body=body, headers=headers)

    def put(
        self,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = "",
        headers: HeadersDataType | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_PUT, request_uri, body=body, headers=headers)

    def delete(
        self,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = "",
        headers: HeadersDataType | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_DELETE, request_uri, body=body, headers=headers)

    def patch(
        self,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = "",
        headers: HeadersDataType | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_PATCH, request_uri, body=body, headers=headers)

    def trace(
        self,
        request_uri: str,
        body: str | bytes | bytearray | memoryview | IO[bytes] | Iterable[bytes] | None = "",
        headers: HeadersDataType | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_TRACE, request_uri, body=body, headers=headers)

    def options(
        self, request_uri: str, headers: Mapping[str, Any] | None = None
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_OPTIONS, request_uri, headers=headers)

    # -- HTTP/2 dispatch (Sprint 3b) --------------------------------------

    HTTP_2 = "HTTP/2.0"

    def request_h2(
        self,
        method: str,
        request_uri: str,
        body: bytes | None = None,
        headers: HeadersDataType | None = None,
        *,
        timeout: float | None = None,
        max_retries: int = 0,
    ) -> Union["HTTP2ResponseHandle", "HTTPSocketPoolResponse"]:
        """Submit a request over HTTP/2 and block until the response
        handle is closed.

        Synchronous convenience over the pull-API of
        :class:`HTTP2ResponseHandle`: spins the pump until the stream
        becomes closed or the timeout expires.

        ALPN-aware auto-fallback (Phase 6 + review_http2_3.md H1):
        when the peer did not negotiate ``h2`` we silently retry on
        the HTTP/1.1 pool. Callers therefore do not need a ``version``
        switch -- opt into HTTP/2 by setting ``http2=True`` on
        the client, and let the transport pick the protocol that
        actually works.

        ``max_retries`` (Sprint 3c) limits automatic retries of
        **connection errors only** for idempotent methods. RFC 9110
        §9.2.2 forbids retries of POST/PATCH after a network error,
        and **timeouts are never retried** -- they indicate that the
        peer may have processed the request but we never saw its
        response, so silently replaying it is unsafe.

        Raises :class:`HTTP2Error` on transport failures (review_http2_3
        H3) so callers can ``except ConnectionError`` uniformly across
        HTTP/1.1 and HTTP/2.

        Behaviour mirrors httpx ``http2=True``: opt-in per client, no
        extra per-request knob.
        """
        if self._h2_pool is None:
            raise HTTP2Error(
                "http2=True must be set on the HTTPClient and the "
                "URL must use https://",
            )
        path = request_uri
        if not path.startswith("/") and not path.startswith("http"):
            path = "/" + path
        merged = self._merge_headers(headers)
        h2_headers = [(k, v) for k, v in merged.items() if k.lower() != "host"]
        authority = merged.get("host") or (
            f"{self.host}:{self.port}" if self.port not in (80, 443) else self.host
        )
        scheme = PROTO_HTTPS if self.ssl else PROTO_HTTP
        port = self.port or (443 if self.ssl else 80)
        # Buffer the body once (in-Memory MVP — Phase 5 may switch to
        # tempfile for large uploads). The buffer is replayed on every
        # retry of an idempotent request.
        body_bytes = bytes(body) if body is not None else None

        # httpx-style auto-fallback (review_http2_3.md H1 + Phase 6):
        # we always try h2 first because the user opted into the h2
        # pool, but if the peer's ALPN chose http/1.1 we close that
        # socket and fall back to the HTTP/1.1 pool. There is no
        # per-request ``version`` knob; opt in via ``http2``.
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        attempts = max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                session = self._h2_pool.get_session(self.host, port, scheme=scheme)
                negotiated = self._alpn_negotiated(session)
                if negotiated and negotiated != "h2":
                    # Peer chose HTTP/1.1: drop exactly this session
                    # (its preface has already been written to the
                    # socket -- the connection is unusable for h1 so
                    # we close the socket and start fresh). Review N1:
                    # drop by identity, not "any pooled session".
                    self._h2_pool.drop_session(session)  # type: ignore[attr-defined]
                    return self._http1_fallback(method, request_uri, body_bytes, headers)
                handle = session.submit_request(
                    method, path, str(authority), h2_headers,
                    scheme=scheme, body=body_bytes,
                )
            except HTTP2ConnectionPoolError as e:
                if "did not negotiate h2" in str(e):
                    return self._http1_fallback(method, request_uri, body_bytes, headers)
                last_exc = e
                if attempt + 1 < attempts and _may_retry_after_send_error(method):
                    continue
                raise HTTP2Error(f"HTTP/2 connection failed: {e}") from e

            try:
                # N3 (review): yield with escalating backoff when the
                # peer sends nothing -- a bare ``gevent.sleep(0)``
                # busy-spins the hub against trickle servers.
                stall = 0.0
                while not handle.is_closed:
                    if deadline is not None and time.monotonic() > deadline:
                        # K2 (review): timeouts surface as HTTP2Error
                        # (a ConnectionError) -- a bare TimeoutError
                        # escaped the ``except ConnectionError``
                        # contract locust-style callers rely on. Never
                        # retried: the peer may have processed the
                        # request (RFC 9110 §9.2.2).
                        raise HTTP2Error(
                            f"HTTP/2 response did not arrive in {timeout}s",
                        )
                    progressed = session.drive_once()
                    if progressed:
                        stall = 0.0
                    else:
                        stall = min(stall + 0.001, 0.05) if stall else 0.001
                        gevent.sleep(stall)
            except HTTP2Error:
                # Timeouts and mapped transport failures surface as-is;
                # retrying a timeout risks double execution.
                raise
            except Exception as e:
                last_exc = e
                if attempt + 1 < attempts and _may_retry_after_send_error(method):
                    continue
                raise
            return handle
        # All attempts failed.
        assert last_exc is not None
        raise HTTP2Error(f"HTTP/2 request failed after {attempts} attempts: {last_exc}")

    def _alpn_negotiated(self, session: HTTP2Session) -> str | None:
        """Return the ALPN protocol the TLS handshake negotiated.

        Reads from the underlying socket of an :class:`HTTP2Session`.
        Returns ``None`` for plaintext sessions or when the peer did
        not advertise ALPN (RFC 7301 says the responder may leave the
        protocol list empty).
        """
        return getattr(session.sock, "selected_alpn_protocol", lambda: None)()

    def _http1_fallback(self, method: str, request_uri: str, body: bytes | None,
                        headers: HeadersDataType | None) -> "HTTPSocketPoolResponse":
        """Route an HTTP/2 attempt over the HTTP/1.1 pool instead.

        Used by :meth:`request_h2` when the peer chose ``http/1.1``
        in the ALPN handshake. The HTTP/1.1 path returns
        a synchronous ``HTTPSocketPoolResponse``; the caller is
        expected to handle either return type via duck-typing.
        """
        # Convert a request_uri-with-leading-slash into the form
        # ``request()`` expects (origin form, no scheme).
        path = request_uri
        if path.startswith(("http://", "https://")):
            # ``request()`` accepts absolute URLs; pass through.
            pass
        elif not path.startswith("/"):
            path = "/" + path
        return self.request(method, path, body=body, headers=headers)

    def _merge_headers(self, headers: HeadersDataType | None) -> Headers:
        merged = self.headers_type()
        merged.update(self.default_headers)
        if headers:
            merged.update(headers)
        return merged


class HTTPClientPool:
    """Factory for maintaining a bunch of clients, one per host:port.

    A client is created on first use and stays until :meth:`close`, which is the
    only way to hand clients back; nothing expires on its own, so the pool grows
    with the number of hosts it has talked to.

    ``http2`` is forwarded to every HTTPClient the pool creates
    (Sprint 5). The pool itself stays HTTP/1.1-shaped: each HTTPClient
    owns its own HTTP2ConnectionPool when ``http2=True``, so
    multiplexed h2 sessions are per-host exactly like h1 sockets.
    """

    def __init__(self, *, http2: bool = False, **kw: Any) -> None:
        self.clients: dict[tuple[str, int | None], HTTPClient] = {}
        self.client_args = {**kw, "http2": http2}

    def get_client(self, url: str | URL) -> HTTPClient:
        if not isinstance(url, URL):
            url = URL(url)
        client_key = url.host, url.port
        try:
            return self.clients[client_key]
        except KeyError:
            client = HTTPClient.from_url(url, **self.client_args)
            self.clients[client_key] = client
            return client

    def close(self) -> None:
        for client in self.clients.values():
            client.close()
        self.clients.clear()
