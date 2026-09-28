from __future__ import annotations

import base64
import os
import select
from collections.abc import Callable
from ssl import PROTOCOL_TLS_CLIENT, get_default_verify_paths
from typing import Any, ClassVar

import gevent.queue
import gevent.socket
import gevent.ssl
from gevent import lock
from gevent.ssl import create_default_context

_certs = get_default_verify_paths()
_CA_CERTS = _certs.cafile or _certs.capath

if not _CA_CERTS or os.path.isdir(_CA_CERTS):
    import certifi

    _CA_CERTS = certifi.where()

_DEFAULT_CIPHERS = (
    "ECDH+AESGCM:DH+AESGCM:ECDH+AES256:DH+AES256:ECDH+AES128:DH+AES:ECDH+HIGH:"
    "DH+HIGH:ECDH+3DES:DH+3DES:RSA+AESGCM:RSA+AES:RSA+HIGH:RSA+3DES:ECDH+RC4:"
    "DH+RC4:RSA+RC4:!aNULL:!eNULL:!MD5"  # codespell-ignore
)


DEFAULT_CONNECTION_TIMEOUT = 5.0
DEFAULT_NETWORK_TIMEOUT = 5.0

IGNORED = object()


class ConnectionPool:
    DEFAULT_CONNECTION_TIMEOUT = 5.0
    DEFAULT_NETWORK_TIMEOUT = 5.0

    def __init__(
        self,
        connection_host: str,
        connection_port: int,
        request_host: str,
        request_port: int,
        size: int = 5,
        disable_ipv6: bool = False,
        connection_timeout: float = DEFAULT_CONNECTION_TIMEOUT,
        network_timeout: float = DEFAULT_NETWORK_TIMEOUT,
        use_proxy: bool = False,
        proxy_user: str | None = None,
        proxy_password: str | None = None,
    ) -> None:
        self._closed = False
        self._connection_host = connection_host
        self._connection_port = connection_port
        self._request_host = request_host
        self._request_port = request_port
        self._semaphore = lock.BoundedSemaphore(size)
        self._socket_queue = gevent.queue.LifoQueue(size)
        self._use_proxy = use_proxy
        self._proxy_credentials: str | None = None
        if proxy_user is not None or proxy_password is not None:
            self._proxy_credentials = f"{proxy_user or ''}:{proxy_password or ''}"

        self.connection_timeout = connection_timeout
        self.network_timeout = network_timeout
        self.size = size
        self.disable_ipv6 = disable_ipv6

    def _resolve(self) -> list[tuple[Any, ...]]:
        """resolve (dns) socket information needed to connect it."""
        family = 0
        if self.disable_ipv6:
            family = gevent.socket.AF_INET
        info = gevent.socket.getaddrinfo(
            self._connection_host,
            self._connection_port,
            family,
            gevent.socket.SOCK_STREAM,
            gevent.socket.SOL_TCP,
        )
        # family, socktype, proto, canonname, sockaddr = info[0]
        return info

    def close(self) -> None:
        self._closed = True
        while not self._socket_queue.empty():
            try:
                sock = self._socket_queue.get(block=False)
                try:
                    sock.close()
                except:  # noqa
                    pass
            except gevent.queue.Empty:
                pass

    def _create_tcp_socket(self, family: int, socktype: int, protocol: int) -> gevent.socket.socket:
        """tcp socket factory."""
        sock = gevent.socket.socket(family, socktype, protocol)
        return sock

    def _create_socket(self) -> gevent.socket.socket:
        """might be overridden and super for wrapping into a ssl socket
        or set tcp/socket options
        """
        sock_infos = self._resolve()
        first_error = None
        for sock_info in sock_infos:
            try:
                sock = self._create_tcp_socket(*sock_info[:3])
            except Exception as e:  # noqa: BLE001
                if not first_error:
                    first_error = e
                continue

            try:
                sock.settimeout(self.connection_timeout)
                sock = self._connect_socket(sock, sock_info[-1])
                self.after_connect(sock)
                sock.settimeout(self.network_timeout)
                return sock
            except OSError as e:
                sock.close()
                if not first_error:
                    first_error = e
            except:
                sock.close()
                raise

        if first_error:
            raise first_error
        else:
            raise RuntimeError(f"Cannot resolve {self._connection_host}:{self._connection_port}")

    def after_connect(self, sock: gevent.socket.socket) -> None:
        pass

    def _connect_socket(self, sock: gevent.socket.socket, address: Any) -> gevent.socket.socket:
        sock.connect(address)
        return sock

    def _proxy_connect_request(self) -> str:
        """Build the CONNECT request line and headers for the proxy tunnel."""
        request = f"CONNECT {self._request_host}:{self._request_port} HTTP/1.1\r\n"
        request += f"Host: {self._request_host}:{self._request_port}\r\n"
        if self._proxy_credentials is not None:
            token = base64.b64encode(self._proxy_credentials.encode("utf-8")).decode("ascii")
            request += f"Proxy-Authorization: Basic {token}\r\n"
        return request + "\r\n"

    def _setup_proxy(self, sock: gevent.socket.socket) -> None:
        """Establish a CONNECT tunnel through the proxy (used for SSL targets).

        Plain HTTP requests are forwarded directly using absolute request URIs
        and do not require a tunnel.
        """
        sock.sendall(self._proxy_connect_request().encode("utf-8"))

        response = b""
        while b"\r\n\r\n" not in response:
            block = sock.recv(4096)
            if not block:
                raise RuntimeError(
                    "Proxy closed the connection before answering the CONNECT request"
                )
            response += block

        status_line = response.split(b"\r\n", 1)[0]
        parts = status_line.split(None, 2)
        if len(parts) < 2 or parts[1] != b"200":
            hint = (
                " (proxy authentication required?)" if len(parts) > 1 and parts[1] == b"407" else ""
            )
            raise RuntimeError(f"Proxy CONNECT failed: {status_line.decode('latin-1')}{hint}")

    def _is_socket_alive(self, sock: gevent.socket.socket | None) -> bool:
        """Check if a socket is still connected and alive.

        Uses select() to check if socket is readable. An idle keep-alive socket
        should NOT be readable.

        Returns False if connection is closed or broken.
        """
        if sock is None:
            return False
        try:
            # Proactive check: A closed socket has a fileno of -1.
            if sock.fileno() < 0:
                return False
            # If the socket is readable while idle, it's either a FIN or dirty.
            ready_to_read, _, _ = select.select([sock], [], [], 0.0)
            return not ready_to_read
        except (OSError, ValueError):
            return False

    def get_socket(self) -> gevent.socket.socket:
        """get a socket from the pool. This blocks until one is available."""
        self._semaphore.acquire()
        if self._closed:
            raise RuntimeError("connection pool closed")

        # Try to get a valid connection from the pool
        while not self._socket_queue.empty():
            try:
                sock = self._socket_queue.get(block=False)
                if self._is_socket_alive(sock):
                    # Connection is still alive, return it
                    return sock
                else:
                    # Connection is dead, close it and try next
                    try:
                        sock.close()
                    except:  # noqa
                        pass
            except gevent.queue.Empty:
                break

        # No valid connections in pool, create a new one
        try:
            return self._create_socket()
        except:
            self._semaphore.release()
            raise

    def return_socket(self, sock: gevent.socket.socket) -> None:
        """return a socket to the pool."""
        if self._closed:
            try:
                sock.close()
            except:  # noqa
                pass
            return
        self._socket_queue.put(sock)
        self._semaphore.release()

    def release_socket(self, sock: gevent.socket.socket) -> None:
        """call when the socket is no more usable."""
        try:
            sock.close()
        except:  # noqa
            pass
        if not self._closed:
            self._semaphore.release()


def init_ssl_context(
    ssl_context_factory: Callable[..., gevent.ssl.SSLContext],
    ca_certs: str | None,
    check_hostname: bool = True,
    ssl_options: dict | None = None,
) -> gevent.ssl.SSLContext:
    """
    Initializes an SSL context with additional SSL options.

    :param ssl_context_factory: Callable to create an SSL context
    :param ca_certs: Path to CA certificates file
    :param check_hostname: Whether to enable hostname checking
    :param ssl_options: Optional dictionary of additional SSL options
    :return: Configured SSLContext instance
    """
    ssl_options = ssl_options or {}

    try:
        ssl_context = ssl_context_factory(cafile=ca_certs)
    except TypeError:
        ssl_context = ssl_context_factory()
        ssl_context.load_verify_locations(cafile=ca_certs)

    ssl_context.check_hostname = check_hostname
    if check_hostname:
        ssl_context.verify_mode = gevent.ssl.CERT_REQUIRED

    if "certfile" in ssl_options and "keyfile" in ssl_options:
        ssl_context.load_cert_chain(
            certfile=ssl_options["certfile"], keyfile=ssl_options["keyfile"]
        )

    if "ciphers" in ssl_options:
        ssl_context.set_ciphers(ssl_options["ciphers"])

    # Apply additional SSL options (e.g., options, verify_flags)
    for option in ["options", "verify_flags"]:
        if option in ssl_options:
            setattr(ssl_context, option, ssl_options[option])

    return ssl_context


class SSLConnectionPool(ConnectionPool):
    """SSLConnectionPool creates connections wrapped with SSL/TLS.

    :param host: hostname
    :param port: port
    :param ssl_options: additional SSL options such as certfile, keyfile,
        ciphers, options and verify_flags
    :param ssl_context_factory: use `ssl.create_default_context` by default
        if provided. It must be a callable that returns a SSLContext.
    """

    default_options: ClassVar[dict] = {
        "ciphers": _DEFAULT_CIPHERS,
        "ca_certs": _CA_CERTS,
        "cert_reqs": gevent.ssl.CERT_REQUIRED,
        "ssl_version": PROTOCOL_TLS_CLIENT,
    }

    def __init__(
        self,
        connection_host: str,
        connection_port: int,
        request_host: str,
        request_port: int,
        insecure: bool = False,
        ssl_context_factory: Callable[..., gevent.ssl.SSLContext] | None = None,
        ssl_options: dict | None = None,
        **kw: Any,
    ) -> None:
        self.insecure = insecure

        self.ssl_options = self.default_options.copy()
        self.ssl_options.update(ssl_options or {})

        self.ssl_context = init_ssl_context(
            ssl_context_factory or create_default_context,
            self.ssl_options["ca_certs"],
            check_hostname=not self.insecure,
            ssl_options=ssl_options,
        )

        super().__init__(connection_host, connection_port, request_host, request_port, **kw)

    def _connect_socket(self, sock: gevent.socket.socket, address: Any) -> gevent.socket.socket:
        sock = super()._connect_socket(sock, address)

        if self._use_proxy:
            self._setup_proxy(sock)

        server_hostname = self.ssl_options.get("server_hostname", self._request_host)
        return self.ssl_context.wrap_socket(sock, server_hostname=server_hostname)
