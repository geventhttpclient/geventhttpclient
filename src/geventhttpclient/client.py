import base64
import errno
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import IO, Any

import gevent.socket

from geventhttpclient import __version__
from geventhttpclient.connectionpool import ConnectionPool, SSLConnectionPool
from geventhttpclient.header import Headers
from geventhttpclient.response import (
    HTTPConnectionClosed,
    HTTPParseError,
    HTTPResponse,
    HTTPSocketPoolResponse,
)
from geventhttpclient.url import URL

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


def _uses_chunked_transfer(header_fields: Mapping[str, Any], body: Any) -> bool:
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
    return not isinstance(body, (bytes, bytearray, memoryview)) and _get_body_length(body) is None


def _requests_100_continue(header_fields: Mapping[str, Any]) -> bool:
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
            block: Any = data[offset : offset + block_size]
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
        headers: Mapping[str, Any] | None = None,
        block_size: int = BLOCK_SIZE,
        connection_timeout: float = ConnectionPool.DEFAULT_CONNECTION_TIMEOUT,
        network_timeout: float = ConnectionPool.DEFAULT_NETWORK_TIMEOUT,
        disable_ipv6: bool = False,
        concurrency: int = 1,
        ssl: bool = False,
        ssl_options: dict | None = None,
        ssl_context_factory: Callable[..., Any] | None = None,
        insecure: bool = False,
        proxy_host: str | None = None,
        proxy_port: int | None = None,
        proxy_user: str | None = None,
        proxy_password: str | None = None,
        version: str = HTTP_11,
        headers_type: type[Headers] = Headers,
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

    def close(self) -> None:
        self._connection_pool.close()

    # Like urllib2, try to treat the body as a file if we can't determine the
    # file length with `len()`

    def _build_request(
        self,
        method: str,
        request_uri: str,
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = b"",
        headers: Mapping[str, Any] | None = None,
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
                header_fields[HEADER_CONTENT_LENGTH] = body_length

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
            request += field + FIELD_VALUE_SEP + str(value) + CRLF
        request += CRLF
        return request

    def request(
        self,
        method: str,
        request_uri: str,
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = b"",
        headers: Mapping[str, Any] | None = None,
    ) -> HTTPSocketPoolResponse:
        """

        :param method:
        :param request_uri:
        :param body: byte or file
        :param headers:
        :return:
        """

        if isinstance(body, str):
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
                _request = request.encode()
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
                            if attempts_left > 0:
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
                        if attempts_left > 0:
                            attempts_left -= 1
                            continue
                        raise
                    response._sent_request = request
                    return response
                if chunked:
                    sock.sendall(_request)
                    # Note: on retry, file-like/iterable bodies continue
                    # from their current position or are exhausted, same
                    # as with `sendfile` before.
                    if body:
                        for block in _iter_chunked(body, self.block_size):
                            sock.sendall(block)
                    else:
                        sock.sendall(b"0\r\n\r\n")
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
                    if attempts_left > 0:
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
                # connection is released by the response itself
                if attempts_left > 0:
                    attempts_left -= 1
                    continue
                raise
            else:
                response._sent_request = request
                return response

    def _send_body_after_continue(
        self, sock: gevent.socket.socket, body: Any, chunked: bool
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
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = "",
        headers: Mapping[str, Any] | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_POST, request_uri, body=body, headers=headers)

    def put(
        self,
        request_uri: str,
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = "",
        headers: Mapping[str, Any] | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_PUT, request_uri, body=body, headers=headers)

    def delete(
        self,
        request_uri: str,
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = "",
        headers: Mapping[str, Any] | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_DELETE, request_uri, body=body, headers=headers)

    def patch(
        self,
        request_uri: str,
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = "",
        headers: Mapping[str, Any] | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_PATCH, request_uri, body=body, headers=headers)

    def trace(
        self,
        request_uri: str,
        body: str | bytes | bytearray | IO[Any] | Iterable[bytes] = "",
        headers: Mapping[str, Any] | None = None,
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_TRACE, request_uri, body=body, headers=headers)

    def options(
        self, request_uri: str, headers: Mapping[str, Any] | None = None
    ) -> HTTPSocketPoolResponse:
        return self.request(METHOD_OPTIONS, request_uri, headers=headers)


class HTTPClientPool:
    """Factory for maintaining a bunch of clients, one per host:port"""

    # TODO: Add some housekeeping and cleanup logic

    def __init__(self, **kw: Any) -> None:
        self.clients: dict[tuple[str, int | None], HTTPClient] = {}
        self.client_args = kw

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
