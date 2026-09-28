import errno
import os

import gevent.socket

from geventhttpclient import __version__
from geventhttpclient.connectionpool import ConnectionPool
from geventhttpclient.header import Headers
from geventhttpclient.response import HTTPConnectionClosed, HTTPParseError, HTTPSocketPoolResponse
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

METHOD_GET = "GET"
METHOD_HEAD = "HEAD"
METHOD_POST = "POST"
METHOD_PUT = "PUT"
METHOD_DELETE = "DELETE"
METHOD_PATCH = "PATCH"
METHOD_OPTIONS = "OPTIONS"
METHOD_TRACE = "TRACE"


def _get_body_length(body):
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


def _uses_chunked_transfer(header_fields, body):
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


def _iter_chunked(body, block_size):
    """Encode the given body with chunked transfer coding (RFC 9112, section 7.1).

    Accepts bytes-like data, a file-like object with `read` or any iterable
    of bytes/str blocks and yields ready-to-send encoded blocks, terminated
    by the final zero-size chunk.
    """
    if isinstance(body, (bytes, bytearray, memoryview)):
        data = memoryview(body)
        for offset in range(0, len(data), block_size):
            block = data[offset : offset + block_size]
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


class HTTPClient:
    HTTP_11 = "HTTP/1.1"
    HTTP_10 = "HTTP/1.0"

    BLOCK_SIZE = 1024 * 4  # 4KB

    DEFAULT_HEADERS = Headers({"User-Agent": "python/gevent-http-client-" + __version__})

    @classmethod
    def from_url(cls, url, **kw):
        if not isinstance(url, URL):
            url = URL(url)
        enable_ssl = url.scheme == PROTO_HTTPS
        if not enable_ssl:
            kw.pop("ssl_options", None)
        return cls(url.host, port=url.port, ssl=enable_ssl, **kw)

    def __init__(
        self,
        host,
        port=None,
        headers=None,
        block_size=BLOCK_SIZE,
        connection_timeout=ConnectionPool.DEFAULT_CONNECTION_TIMEOUT,
        network_timeout=ConnectionPool.DEFAULT_NETWORK_TIMEOUT,
        disable_ipv6=False,
        concurrency=1,
        ssl=False,
        ssl_options=None,
        ssl_context_factory=None,
        insecure=False,
        proxy_host=None,
        proxy_port=None,
        version=HTTP_11,
        headers_type=Headers,
    ):
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
        else:
            self.use_proxy = False
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
            # Import SSL as late as possible, fail hard with Import Error
            from geventhttpclient.connectionpool import SSLConnectionPool

            self._connection_pool = SSLConnectionPool(
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

    def close(self):
        self._connection_pool.close()

    # Like urllib2, try to treat the body as a file if we can't determine the
    # file length with `len()`

    def _build_request(self, method, request_uri, body="", headers=None, chunked=None):
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
        if self.use_proxy:
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

    def request(self, method, request_uri, body=b"", headers=None):
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

        request = self._build_request(
            method.upper(), request_uri, body=body, headers=headers, chunked=chunked
        )

        attempts_left = self._connection_pool.size + 1

        while 1:
            sock = self._connection_pool.get_socket()
            try:
                _request = request.encode()
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
            except HTTPConnectionClosed as e:
                # connection is released by the response itself
                if attempts_left > 0:
                    attempts_left -= 1
                    continue
                raise e
            else:
                response._sent_request = request
                return response

    def get(self, request_uri, headers={}):
        return self.request(METHOD_GET, request_uri, headers=headers)

    def head(self, request_uri, headers=None):
        return self.request(METHOD_HEAD, request_uri, headers=headers)

    def post(self, request_uri, body="", headers=None):
        return self.request(METHOD_POST, request_uri, body=body, headers=headers)

    def put(self, request_uri, body="", headers=None):
        return self.request(METHOD_PUT, request_uri, body=body, headers=headers)

    def delete(self, request_uri, body="", headers=None):
        return self.request(METHOD_DELETE, request_uri, body=body, headers=headers)

    def patch(self, request_uri, body="", headers=None):
        return self.request(METHOD_PATCH, request_uri, body=body, headers=headers)

    def trace(self, request_uri, body="", headers=None):
        return self.request(METHOD_TRACE, request_uri, body=body, headers=headers)

    def options(self, request_uri, headers=None):
        return self.request(METHOD_OPTIONS, request_uri, headers=headers)


class HTTPClientPool:
    """Factory for maintaining a bunch of clients, one per host:port"""

    # TODO: Add some housekeeping and cleanup logic

    def __init__(self, **kw):
        self.clients = {}
        self.client_args = kw

    def get_client(self, url):
        if not isinstance(url, URL):
            url = URL(url)
        client_key = url.host, url.port
        try:
            return self.clients[client_key]
        except KeyError:
            client = HTTPClient.from_url(url, **self.client_args)
            self.clients[client_key] = client
            return client

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()
