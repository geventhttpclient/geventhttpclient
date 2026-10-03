"""Tiny HTTP/2 test server backed by ``h2`` + ``gevent``.

Spawned as a per-test greenlet by ``tests/test_http2_spec.py``.
Listens on ``127.0.0.1`` with TLS+ALPN ``h2`` and a fixed
self-signed certificate. The behaviour per request is configured
via the ``config`` argument the harness passes in -- most tests only
need the default echo-ish behaviour, but ``routing`` lets us customise
the responses per path for specific RFC-feature tests.

Design goals:

* Sans-IO at the protocol level: every frame is generated through
  ``h2.connection.H2Connection`` events, never raw bytes.
* Co-operative scheduling: the server greenlet ``yield``s after
  every read/write so the test main greenlet can drive its assertions
  in between.
* Idempotent setup: each test starts its own server so failures in
  one test do not pollute the next.
"""

from __future__ import annotations

import os

# Default location of the test certs. Falls back to ``tests/certs/``
# so contributors without ``/tmp/pi/nginx/`` can still run the suite
# against the bundled self-signed cert.
import os as _os
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Self

import gevent
import gevent.ssl
import h2.config
import h2.connection
import h2.events
import h2.exceptions

_HERE = _os.path.dirname(_os.path.abspath(__file__))
CERT_FILE = _os.environ.get(
    "GEVENTHTTPCLIENT_TEST_CERT",
    "/tmp/pi/nginx/server.crt"
    if _os.path.exists("/tmp/pi/nginx/server.crt")
    else _os.path.join(_HERE, "certs", "server.crt"),
)
KEY_FILE = _os.environ.get(
    "GEVENTHTTPCLIENT_TEST_KEY",
    "/tmp/pi/nginx/server.key"
    if _os.path.exists("/tmp/pi/nginx/server.key")
    else _os.path.join(_HERE, "certs", "server.key"),
)

# Default response builder: returns ``{"status": int, "headers": list,
# "body": bytes}`` for a given request. Tests can replace this via the
# ``routing`` argument.
RequestKey = tuple[str, dict[str, str]]  # (path, lowercased headers dict)


def _make_response(
    method: str,
    path: str,
    headers: Iterable[tuple[str, str]],
    body_in: bytes,
) -> dict[str, object]:
    """Default echo-ish response."""
    return {
        "status": 200,
        "headers": [("content-type", "text/plain")],
        "body": f"echo {method} {path}\n".encode() + body_in,
    }


@dataclass
class H2ServerConfig:
    """Test-server knobs."""

    handler: Callable[[str, str, list[tuple[str, str]], bytes], dict[str, object]] = _make_response
    initial_settings: dict[str, int] = field(default_factory=dict)
    # ``extra_headers`` is appended to every response (e.g. to set
    # ``server`` for debug output).
    extra_headers: list[tuple[str, str]] = field(default_factory=list)


class H2TestServer:
    """A co-operative HTTP/2 test server backed by ``h2``.

    Use as a context manager -- on exit the greenlet is killed and
    the listening socket closed::

        with H2TestServer(("127.0.0.1", 0)) as server:
            addr, port = server.address
            ... do client request against addr:port ...
    """

    def __init__(
        self,
        listen: tuple[str, int] = ("127.0.0.1", 0),
        *,
        config: H2ServerConfig | None = None,
    ) -> None:
        self.config = config or H2ServerConfig()
        self._listen_addr = listen
        self._sock: gevent.socket.socket | None = None
        self._acceptor: gevent.Greenlet | None = None
        self._active_connections: list[gevent.Greenlet] = []
        # Live client sockets, so ``stop()`` can break handlers out of
        # a parked ``recv()`` via ``shutdown()`` instead of killing the
        # greenlets mid-I/O (killing them left the libuv loop on
        # Windows in a state where later accept watchers never fired --
        # the next test's StreamServer hung forever).
        self._client_socks: set[gevent.ssl.SSLSocket] = set()
        self._stop_event = gevent.event.Event()
        self._client_connection: h2.connection.H2Connection | None = None

    @property
    def address(self) -> tuple[str, int]:
        assert self._sock is not None, "server not started"
        return self._sock.getsockname()[:2]

    @property
    def port(self) -> int:
        return self.address[1]

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.stop()

    def start(self) -> None:
        if not os.path.exists(CERT_FILE) or not os.path.exists(KEY_FILE):
            raise RuntimeError(
                f"missing test certs at {CERT_FILE} and {KEY_FILE}; "
                "generate them with openssl before running h2 tests"
            )
        # Explicit server-side context (the modern idiom instead of
        # ``create_default_context(Purpose.CLIENT_AUTH)``): no hostname
        # verification semantics, server cert via ``load_cert_chain``.
        ctx = gevent.ssl.SSLContext(gevent.ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
        ctx.set_alpn_protocols(["h2", "http/1.1"])
        # Self-signed cert is the only thing we offer; tests opt in via
        # ``insecure=True`` on the client.
        self._sock = gevent.socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(self._listen_addr)
        self._sock.listen(64)
        self._acceptor = gevent.spawn(self._accept_loop, ctx)
        # Wait for the listening socket to be ready.
        deadline = gevent.hub.get_hub().loop.now() + 5.0
        while self._sock.fileno() < 0 and gevent.hub.get_hub().loop.now() < deadline:
            gevent.sleep(0)

    def stop(self) -> None:
        """Cooperative shutdown.

        Never kills a greenlet that is parked inside a socket call:
        on Windows/libuv, ``GreenletExit`` delivered into a pending
        accept/recv leaves the loop's watcher in a state where later
        servers never observe their events (the accept watcher is
        retired only on a loop tick). Instead we (1) signal the
        acceptor, which polls ``_stop_event`` on a short accept
        timeout, (2) break parked handlers via ``shutdown()`` so they
        unwind themselves through their ``finally``, and (3) wait for
        both to finish -- killing only as a last resort and then
        blocking, so unwinding completes before we return. A final
        loop tick retires the watcher (same workaround as
        ``tests/common.py``).
        """
        self._stop_event.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        # Break handlers parked in recv(): a half-close makes recv
        # return b"" / raise, and each handler closes its client in
        # its own ``finally``.
        for sock in list(self._client_socks):
            try:
                sock.shutdown(gevent.socket.SHUT_RDWR)
            except OSError:
                pass
        self._join_all(self._active_connections, per_greenlet=5.0)
        if self._acceptor is not None:
            self._acceptor.join(timeout=5.0)
            if not self._acceptor.ready():
                # Acceptor ignored the stop signal -- force it, but
                # block so the GreenletExit unwinds right now.
                self._acceptor.kill(block=True, timeout=1.0)
        self._active_connections.clear()
        self._client_socks.clear()
        # libuv on Windows needs a loop tick to retire the accept
        # watcher, otherwise the next server never accepts (see
        # ``tests/common.py`` for the same workaround).
        gevent.sleep(0.001)

    @staticmethod
    def _join_all(
        greenlets: list[gevent.Greenlet], *, per_greenlet: float,
    ) -> None:
        for g in greenlets:
            g.join(timeout=per_greenlet)
            if not g.ready():
                g.kill(block=True, timeout=1.0)

    def _accept_loop(self, ctx: gevent.ssl.SSLContext) -> None:
        assert self._sock is not None
        # Short accept timeout: the acceptor must wake up regularly to
        # observe ``_stop_event`` instead of being parked in accept()
        # when ``stop()`` closes the listening socket under it.
        self._sock.settimeout(0.1)
        while not self._stop_event.is_set():
            try:
                client, _ = self._sock.accept()
            except gevent.socket.timeout:
                continue
            except OSError:
                return
            try:
                client = ctx.wrap_socket(client, server_side=True)
            except (OSError, gevent.ssl.SSLError):
                try:
                    client.close()
                except Exception:
                    pass
                continue
            if client.selected_alpn_protocol() != "h2":
                try:
                    client.close()
                except Exception:
                    pass
                continue
            self._client_socks.add(client)
            g = gevent.spawn(self._handle_connection, client)
            self._active_connections.append(g)

    def _handle_connection(self, client: gevent.ssl.SSLSocket) -> None:
        config = h2.config.H2Configuration(
            client_side=False,
            header_encoding="utf-8",
        )
        h2_conn = h2.connection.H2Connection(config=config)
        h2_conn.initiate_connection()
        client.sendall(h2_conn.data_to_send())
        self._client_connection = h2_conn
        # Body buffers keyed by stream-id. ``h2`` does not retain DATA
        # frames across ``receive_data`` boundaries, so we accumulate
        # them ourselves and only hand the bytes to the handler once
        # the request ends.
        bodies: dict[int, bytearray] = {}
        pending: dict[int, h2.events.RequestReceived] = {}
        try:
            while not self._stop_event.is_set():
                try:
                    data = client.recv(65536)
                except (OSError, gevent.ssl.SSLError):
                    return
                if not data:
                    return
                events = h2_conn.receive_data(data)
                for event in events:
                    if isinstance(event, h2.events.RequestReceived):
                        # Stash; we will dispatch the handler when the
                        # body is complete (``StreamEnded``).
                        pending[event.stream_id] = event
                        bodies.setdefault(event.stream_id, bytearray())
                    elif isinstance(event, h2.events.DataReceived):
                        buf = bodies.setdefault(event.stream_id, bytearray())
                        buf.extend(event.data)
                    elif isinstance(event, h2.events.StreamEnded):
                        req = pending.pop(event.stream_id, None)
                        body = bytes(bodies.pop(event.stream_id, bytearray()))
                        if req is not None:
                            self._handle_request(h2_conn, req, body)
                    elif isinstance(event, h2.events.PingReceived):
                        # ``h2`` auto-acks pings; nothing to do.
                        pass
                    elif isinstance(event, h2.events.WindowUpdated):
                        pass
                    elif isinstance(event, h2.events.ConnectionTerminated):
                        return
                    elif isinstance(event, h2.events.StreamReset):
                        # Client cancelled the stream. ``h2`` has
                        # already updated internal state.
                        bodies.pop(event.stream_id, None)
                        pending.pop(event.stream_id, None)
                    elif isinstance(
                        event,
                        (h2.events.RemoteSettingsChanged, h2.events.SettingsAcknowledged),
                    ):
                        pass
                client.sendall(h2_conn.data_to_send())
                gevent.sleep(0)
        finally:
            self._client_socks.discard(client)
            try:
                client.close()
            except Exception:
                pass

    def _handle_request(
        self,
        h2_conn: h2.connection.H2Connection,
        event: h2.events.RequestReceived,
        body: bytes,
    ) -> None:
        cfg = self.config
        headers_list = list(event.headers)
        # The h2 RequestReceived event does not expose ``path`` /
        # ``method`` as separate attributes -- everything lives in
        # the headers tuple, and pseudo-headers are lowercased.
        method = ""
        path = ""
        for name, value in headers_list:
            if name == ":method":
                method = value
            elif name == ":path":
                path = value
        result = cfg.handler(method, path, headers_list, body)
        status = int(result.get("status", 200))  # type: ignore[arg-type]
        out_headers: list[tuple[str, str]] = list(result.get("headers", []))  # type: ignore[arg-type]
        out_headers.extend(cfg.extra_headers)
        out_body = bytes(result.get("body", b""))  # type: ignore[arg-type]
        # 1xx informational responses (RFC 9113 §8.1.1): the handler
        # may return a list of ``{"status": int, "headers": [...]}``
        # blocks in ``result["informational"]`` to precede the final
        # response. The default echo handler emits none.
        for info in result.get("informational", []) or []:  # type: ignore[union-attr]
            info_status = int(info.get("status", 100))  # type: ignore[arg-type,union-attr]
            info_headers = list(info.get("headers", []))  # type: ignore[arg-type,union-attr]
            h2_conn.send_headers(
                stream_id=event.stream_id,
                headers=[(":status", str(info_status)), *info_headers],
            )
        # If the handler wanted to send trailers, it would put them in
        # ``result["trailers"]``; default: none.
        trailers: list[tuple[str, str]] = list(result.get("trailers", []))  # type: ignore[arg-type]
        h2_conn.send_headers(
            stream_id=event.stream_id,
            headers=[(":status", str(status)), *out_headers],
        )
        if out_body:
            # Split into two frames so tests can exercise multi-frame
            # DATA reception. A no-body request skips this entirely.
            mid = len(out_body) // 2
            if mid > 0:
                h2_conn.send_data(
                    stream_id=event.stream_id,
                    data=out_body[:mid],
                    end_stream=not trailers and mid == len(out_body),
                )
                h2_conn.send_data(
                    stream_id=event.stream_id,
                    data=out_body[mid:],
                    end_stream=not trailers,
                )
            else:
                # mid == 0 -> send all in one frame.
                h2_conn.send_data(
                    stream_id=event.stream_id,
                    data=out_body,
                    end_stream=not trailers,
                )
        elif not trailers:
            h2_conn.end_stream(event.stream_id)
        if trailers:
            h2_conn.send_headers(
                stream_id=event.stream_id,
                headers=trailers,
                end_stream=True,
            )


__all__ = ["H2ServerConfig", "H2TestServer", "_make_response"]
