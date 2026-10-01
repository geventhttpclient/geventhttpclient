"""
Provide HTTPConnection, HTTPSConnection and HTTPResponse implementations ready
to use as drop-in replacements for their counterparts in http.client.
"""

import http.client
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import gevent.socket
import gevent.ssl

from geventhttpclient import connectionpool, header, response

_UNKNOWN = getattr(http.client, "_UNKNOWN", "UNKNOWN")
# the sentinel http.client uses for "inherit the global default timeout"
_GLOBAL_DEFAULT_TIMEOUT: Any = getattr(socket, "_GLOBAL_DEFAULT_TIMEOUT", None)


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

    def items(self) -> Iterator[tuple[str, Any]]:
        # Responses that are not stdlib http.client.HTTPResponse instances lose
        # their status when code like httplib2.Response copies them from the
        # items: it then defaults to 200 and never follows redirects or raises
        # for error statuses. Yield the status line as a pseudo header so the
        # status survives the copy.
        yield "status", self.status_code
        yield from super().items()

    # For compatibility with old-style urllib responses. cookielib etc.

    def geturl(self) -> str | None:
        return self.url

    def getcode(self) -> int:
        return self.status_code


class HTTPConnection(http.client.HTTPConnection):
    # HTTPResponse here is ours, not http.client's, so no shared type
    response_class: Any = HTTPResponse
    source_address: Any = None
    _hidden_socket: gevent.socket.socket | None = None

    def connect(self) -> None:
        self.sock = gevent.socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        # private stdlib attributes, not covered by the typeshed declaration
        if self._tunnel_host:  # type: ignore[attr-defined]
            self._tunnel()  # type: ignore[attr-defined]

    def getresponse(self) -> HTTPResponse:  # type: ignore[override]
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


def _create_https_context(http_vsn: int) -> gevent.ssl.SSLContext:
    """Build the context a connection uses when the caller brings none.

    Mirrors ``http.client._create_https_context``, which 3.12 and newer keep as
    a module function while 3.10 and 3.11 built the same context inline.  The
    one deviation is the trust store: ours points the context at
    ``connectionpool._CA_CERTS``, which is the system bundle where there is one
    and certifi where there is not.
    """
    context = connectionpool.init_ssl_context(
        gevent.ssl.create_default_context,
        connectionpool._CA_CERTS,
        check_hostname=True,
    )
    # send ALPN extension to indicate HTTP/1.1 protocol
    if http_vsn == 11:
        context.set_alpn_protocols(["http/1.1"])
    # enable PHA for TLS 1.3 connections if available
    if context.post_handshake_auth is not None:
        context.post_handshake_auth = True
    return context


class HTTPSConnection(HTTPConnection):
    """A gevent powered ``http.client.HTTPSConnection``.

    The keyword arguments and the attributes follow http.client as closely as
    the supported CPython versions allow.  3.10 and 3.11 take ``key_file``,
    ``cert_file`` and ``check_hostname`` here and deprecated them; 3.12 dropped
    all three in favour of a context.  We accept every one of them, working, so
    that code written against either shape keeps going once http.client is
    patched, and behaves the same on every version in between.
    """

    default_port = 443

    def __init__(
        self,
        host: str,
        port: int | None = None,
        key_file: str | None = None,
        cert_file: str | None = None,
        timeout: Any = _GLOBAL_DEFAULT_TIMEOUT,
        source_address: tuple[str, int] | None = None,
        *,
        context: gevent.ssl.SSLContext | None = None,
        check_hostname: bool | None = None,
        **kw: Any,
    ) -> None:
        super().__init__(
            host,
            port,
            timeout=timeout,
            source_address=source_address,
            **kw,
        )
        if key_file is not None or cert_file is not None or check_hostname is not None:
            import warnings

            warnings.warn(
                "key_file, cert_file and check_hostname are "
                "deprecated, use a custom context instead.",
                DeprecationWarning,
                2,
            )
        self.key_file = key_file
        self.cert_file = cert_file
        if context is None:
            context = _create_https_context(self._http_vsn)  # type: ignore[attr-defined]
        will_verify = context.verify_mode != gevent.ssl.CERT_NONE
        if check_hostname is None:
            check_hostname = context.check_hostname
        if check_hostname and not will_verify:
            raise ValueError(
                "check_hostname needs a SSL context with either CERT_OPTIONAL or CERT_REQUIRED"
            )
        if key_file or cert_file:
            context.load_cert_chain(cert_file, key_file)
            # cert and key file means the user wants to authenticate.
            # enable TLS 1.3 PHA implicitly even for custom contexts.
            if context.post_handshake_auth is not None:
                context.post_handshake_auth = True
        self._context = context
        if check_hostname is not None:
            self._context.check_hostname = check_hostname

    def connect(self) -> None:
        """Connect to a host on a given (SSL) port."""

        sock = gevent.socket.create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:  # type: ignore[attr-defined]
            self.sock = sock
            self._tunnel()  # type: ignore[attr-defined]
        # through a proxy the tunnel is what the certificate has to name
        server_hostname: str = self._tunnel_host or self.host  # type: ignore[attr-defined]
        self.sock = gevent.ssl.SSLSocket(
            sock, _context=self._context, server_hostname=server_hostname
        )


def patch() -> None:
    http.client.HTTPConnection = HTTPConnection  # type: ignore[misc]
    http.client.HTTPResponse = HTTPResponse  # type: ignore[misc,assignment]
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
        http.client.HTTPResponse = HTTPResponse  # type: ignore[misc,assignment]
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
