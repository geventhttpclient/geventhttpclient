"""HTTP/2 connection pool (Sprint 3b).

A multiplexed h2 connection has different lifecycle from a HTTP/1.1 socket:
* it stays open across many requests,
* "releasing" a request is a no-op (the connection is shared),
* closing sends a graceful GOAWAY and only then closes the underlying
 TCP/SSL socket.

The :class:`HTTP2ConnectionPool` here owns one HTTP/2 session per
``(host, port, scheme)`` triple. The pool only spawns new sessions
when a request needs a different endpoint; it does **not** spawn
its own greenlets — the caller is expected to drive each session
externally (typically one per host:port) or via the synchronous API
in :mod:`geventhttpclient.http2.session` which loops ``drive_once()``
until the response handle closes.

The pool is intentionally small: it does **not** try to limit
concurrent sessions or enforce a maximum. Sprint 3c adds eviction
and pool-level locking if needed.
"""

from dataclasses import dataclass

import gevent.lock
import gevent.socket
import gevent.ssl

from geventhttpclient.http2._core import HTTP2Connection
from geventhttpclient.http2.session import HTTP2Session


class HTTP2ConnectionPoolError(ConnectionError):
    """Raised when a session lookup or handshake fails.

    ``ConnectionError`` base so ``except ConnectionError`` catches it
    uniformly with the h1 transport (review K2). The ALPN-mismatch
    message is matched by ``HTTPClient.request_h2`` for the
    transparent HTTP/1.1 fallback.
    """


@dataclass(frozen=True)
class _PoolKey:
    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


class HTTP2ConnectionPool:
    """Pool of multiplexed HTTP/2 sessions, keyed by endpoint.

    Public methods:
    :meth:`get_session` returns the session for ``(host, port, scheme)``,
    creating it lazily on first use. :meth:`release_session` is a
    no-op (the session stays open for more requests). :meth:`close`
    sends graceful GOAWAYs on every live session and shuts the
    underlying sockets.
    """

    def __init__(
        self,
        *,
        connection_timeout: float = 5.0,
        network_timeout: float = 5.0,
        insecure: bool = False,
    ) -> None:
        self._sessions: dict[_PoolKey, HTTP2Session] = {}
        # ``gevent.lock.Lock`` cooperates with the gevent hub; a plain
        # ``threading.Lock`` would deadlock if the section between
        # ``acquire`` and ``release`` yields (e.g. during DNS or TLS).
        # ``gevent.lock`` keeps the API identical so the call sites
        # do not change.
        self._lock = gevent.lock.RLock()
        self.connection_timeout = connection_timeout
        self.network_timeout = network_timeout
        self.insecure = insecure
        self._closed = False

    def get_session(self, host: str, port: int, *, scheme: str = "https") -> HTTP2Session:
        """Return the h2 session for the given endpoint, opening one if
        necessary.

        On first use the socket is opened, wrapped with TLS+ALPN
        (``h2, http/1.1``) and the session preface + initial SETTINGS
        are flushed. Subsequent calls reuse the same session.
        """
        if self._closed:
            raise HTTP2ConnectionPoolError("pool closed")

        key = _PoolKey(scheme=scheme, host=host, port=port)
        session = self._sessions.get(key)
        if session is not None:
            return session

        with self._lock:
            session = self._sessions.get(key)
            if session is not None:
                return session
            sock = self._open_socket(host, port)
            session = HTTP2Session(sock, HTTP2Connection())
            # Flush preface + initial SETTINGS.
            session.flush_outbound()
            self._sessions[key] = session
            return session

    def release_session(self, session: HTTP2Session) -> None:
        """No-op for h2 — the session stays open for further requests.

        Kept on the API for parity with :class:`ConnectionPool`. Sprint
        3c may revisit this to drain half-closed streams on shutdown.
        """

    def drop_session(self, session: HTTP2Session) -> None:
        """Remove exactly ``session`` from the pool and close its socket.

        Used by ``HTTPClient.request_h2`` when a session turns out to
        be unusable (ALPN resolved to ``http/1.1`` after the preface
        was already written). Review N1: the previous code popped
        ``next(iter(self._sessions))`` -- an arbitrary session when
        more than one host is pooled. This method removes by identity
        via a reverse lookup under the pool lock.
        """
        with self._lock:
            key = next(
                (k for k, s in self._sessions.items() if s is session),
                None,
            )
            if key is None:
                return
            del self._sessions[key]
        # ``close_sock`` swallows its own errors; the pool's job here
        # is to detach the session and best-effort close the socket.
        session.close_sock()

    def close(self) -> None:
        """Close every session. The pool refuses further requests after."""
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._closed = True
        for session in sessions:
            try:
                session.connection.submit_goaway(0, 0, b"close")
                session.flush_outbound()
            except Exception:  # noqa: BLE001,S110
                pass
            # ``close_sock`` swallows its own errors.
            session.close_sock()

    def active_sessions(self) -> int:
        """How many h2 sessions this pool is currently keeping.

        Exposed for diagnostics and tests; not used for backpressure
        (the per-session MAX_CONCURRENT_STREAMS gate handles that).
        """
        return len(self._sessions)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open_socket(self, host: str, port: int) -> gevent.socket.socket:
        """Open a TCP socket (and wrap in TLS+ALPN ``h2`` if needed).

        HTTP/2 over plaintext (h2c) is not supported in this pool — the
        web has converged on ``https://`` for h2, and nghttp2's
        prior-knowledge mode would need a separate code path.
        """
        sock = gevent.socket.create_connection(
            (host, port), timeout=self.connection_timeout,
        )
        sock.settimeout(self.network_timeout)
        ctx = gevent.ssl.create_default_context()
        if self.insecure:
            ctx.check_hostname = False
            ctx.verify_mode = gevent.ssl.CERT_NONE
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        sock = ctx.wrap_socket(sock, server_hostname=host)
        if sock.selected_alpn_protocol() != "h2":
            sock.close()
            raise HTTP2ConnectionPoolError(
                f"server at {host}:{port} did not negotiate h2 "
                f"(got {sock.selected_alpn_protocol()!r})",
            )
        return sock


__all__ = ["HTTP2ConnectionPool", "HTTP2ConnectionPoolError"]
