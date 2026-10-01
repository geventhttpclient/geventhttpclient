"""
Provide HTTPConnection, HTTPSConnection and HTTPResponse implementations ready
to use as drop-in replacements for their counterparts in http.client.
"""

import http.client
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import gevent.socket
import gevent.ssl

from geventhttpclient import connectionpool, header, response

_UNKNOWN = getattr(http.client, "_UNKNOWN", "UNKNOWN")


class HTTPLibHeaders(header.Headers):
    def __getitem__(self, key: str | bytes) -> Any:
        value = super().__getitem__(key)
        if isinstance(value, (list, tuple)):
            return ", ".join(value)
        else:
            return value


class HTTPResponse(response.HTTPSocketResponse):
    # declared (annotation only) to break the getter/setter inference cycle
    _msg: HTTPLibHeaders

    def __init__(
        self,
        sock: gevent.socket.socket,
        debuglevel: int = 0,
        method: str | None = None,
        url: str | None = None,
        **kw: Any,
    ) -> None:
        method = "GET" if method is None else method.upper()
        super().__init__(sock, method=method, **kw)
        self.url = url
        self.chunked = _UNKNOWN
        self.chunk_left = _UNKNOWN

    @property
    def msg(self) -> HTTPLibHeaders:
        if hasattr(self, "_msg"):
            return self._msg
        self._msg = HTTPLibHeaders(self._headers_index)
        return self._msg

    @msg.setter
    def msg(self, headers: HTTPLibHeaders) -> None:
        # required by do_open()
        self._msg = headers

    @property
    def fp(self) -> "HTTPResponse":
        return self

    @property
    def version(self) -> int:  # type: ignore[override]
        # http.client reports the version as an int, our python base as a string
        v = self.get_http_version()
        if v == "HTTP/1.1":
            return 11
        return 10

    @property
    def status(self) -> int:
        return self.status_code

    @property
    def code(self) -> int:
        return self.status_code

    @property
    def reason(self) -> HTTPLibHeaders:
        return self.msg

    def _read_status(self) -> tuple[int, int, HTTPLibHeaders]:
        return (self.version, self.status_code, self.msg)

    def begin(self) -> None:
        pass

    def flush(self) -> None:
        self._body_buffer.clear()

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self.release()

    def isclosed(self) -> bool:
        return self._sock is None

    def read(self, amt: int | None = None) -> bytes:
        # the parameter is named amt to match http.client.HTTPResponse, our
        # python base class calls it length
        return super().read(amt)

    def readinto(self, b: bytearray) -> int:
        raise NotImplementedError()

    def read1(self, n: int = -1) -> bytes:
        raise NotImplementedError()

    def peek(self, n: int = -1) -> bytes:
        raise NotImplementedError()

    def fileno(self) -> int:
        raise NotImplementedError()

    def getheader(self, name: str, default: Any = None) -> Any:
        return self.get(name.lower(), default)

    def getheaders(self) -> list[tuple[str, Any]]:
        return list(self._headers_index.items())

    @property
    def will_close(self) -> bool:
        return self.message_complete and not self.should_keep_alive()

    def _check_close(self) -> bool:
        return not self.should_keep_alive()

    # For compatibility with old-style urllib responses. cookielib etc.

    def geturl(self) -> str | None:
        return self.url

    def getcode(self) -> int:
        return self.status_code


class HTTPConnection(http.client.HTTPConnection):
    response_class = HTTPResponse
    source_address: Any = None
    _hidden_socket: gevent.socket.socket | None = None

    def connect(self) -> None:
        self.sock = gevent.socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        # private stdlib attributes, not covered by the typeshed declaration
        if self._tunnel_host:  # type: ignore[attr-defined]
            self._tunnel()  # type: ignore[attr-defined]

    def getresponse(self) -> HTTPResponse:
        # For recent python versions urllib.request.AbstractHTTPHandler.do_open()
        # insists on closing the socket prematurely, right after receiving a response.
        # So in our case, right after just reading the HTTP headers, the socket gets
        # killed. Therefore, we have two options:
        #
        # 1. We read everything into some buffer, before returning the response object
        # 2. We stop do_open() from messing around with the socket
        #
        # As HTTPSocketResponse is not really intended to buffer big chunks of data,
        # option two remains. Better ideas wellcome.

        if not self.sock and self._hidden_socket is not None:
            self.sock = self._hidden_socket
        resp = super().getresponse()
        self._hidden_socket = self.sock
        self.sock = None
        return resp  # type: ignore[return-value]

    def close(self) -> None:
        if not self.sock and self._hidden_socket is not None:
            self.sock = self._hidden_socket
        super().close()


class HTTPSConnection(HTTPConnection):
    default_port = 443

    def __init__(
        self,
        host: str,
        port: int | None = None,
        key_file: str | None = None,
        cert_file: str | None = None,
        context: gevent.ssl.SSLContext | None = None,
        check_hostname: bool | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(host, port, **kw)
        if key_file is not None or cert_file is not None or check_hostname is not None:
            import warnings

            warnings.warn(
                "key_file, cert_file and check_hostname are "
                "deprecated, use a custom context instead.",
                DeprecationWarning,
                2,
            )
        # Kept for compatibility: code outside us sets these two attributes on
        # the connection it is handed and reads them back, the same way it does
        # for http.client connections. Neither is used to build the context, the
        # certificate goes into it and the key never was read here.
        self.key_file = key_file
        self.cert_file = cert_file or connectionpool._CA_CERTS
        if context is None:
            context = connectionpool.init_ssl_context(
                gevent.ssl.create_default_context,
                self.cert_file,
                check_hostname=check_hostname,  # type: ignore[arg-type]
            )
            # send ALPN extension to indicate HTTP/1.1 protocol
            if self._http_vsn == 11:  # type: ignore[attr-defined]
                context.set_alpn_protocols(["http/1.1"])
            # enable PHA for TLS 1.3 connections if available
            if context.post_handshake_auth is not None:
                context.post_handshake_auth = True
        self._context = context

    def connect(self) -> None:
        """Connect to a host on a given (SSL) port."""

        sock = gevent.socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:  # type: ignore[attr-defined]
            self.sock = sock
            self._tunnel()  # type: ignore[attr-defined]
        self.sock = gevent.ssl.SSLSocket(sock, _context=self._context, server_hostname=self.host)


def patch() -> None:
    http.client.HTTPConnection = HTTPConnection  # type: ignore[misc]
    http.client.HTTPResponse = HTTPResponse  # type: ignore[misc]
    try:
        http.client.HTTPSConnection = HTTPSConnection  # type: ignore[misc,assignment]
    except NameError:
        pass


@contextmanager
def patched() -> Iterator[None]:
    """Temporarily patch http.client."""
    http_client_HTTPConnection = http.client.HTTPConnection
    http_client_HTTPResponse = http.client.HTTPResponse
    try:
        http_client_HTTPSConnection = http.client.HTTPSConnection
    except NameError:
        pass
    try:
        http.client.HTTPConnection = HTTPConnection  # type: ignore[misc]
        http.client.HTTPResponse = HTTPResponse  # type: ignore[misc]
        try:
            http.client.HTTPSConnection = HTTPSConnection  # type: ignore[misc,assignment]
        except NameError:
            pass
        yield
    finally:
        http.client.HTTPConnection = http_client_HTTPConnection  # type: ignore[misc]
        http.client.HTTPResponse = http_client_HTTPResponse  # type: ignore[misc]
        try:
            # no incompatible type error here, the saved value is a subclass
            http.client.HTTPSConnection = http_client_HTTPSConnection  # type: ignore[misc]
        except NameError:
            pass
