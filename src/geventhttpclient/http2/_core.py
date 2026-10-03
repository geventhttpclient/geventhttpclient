"""Sans-IO HTTP/2 connection layer.

Wraps :mod:`geventhttpclient._http2_parser` (the raw nghttp2 binding) and
adds:

* Typed ``@dataclass`` events instead of ``dict``-shaped records.
* A per-stream ``StreamState`` aggregate with HTTP/2 state-machine
  bookkeeping (RFC 9113 §5.1).
* A :class:`HTTP2Connection` facade exposing the call shape users need:
  ``feed()`` to push inbound bytes, ``submit_*`` to push requests,
  ``bytes_to_send()`` to drain outbound frames, and per-stream
  :attr:`~HTTP2Connection.streams` introspection.
* A ``MAX_CONCURRENT_STREAMS`` gate that raises :exc:`BlockingIOError`
  (asyncio-compatible) instead of silently dropping them — gevent
  layers can translate that into real blocking in Phase 4.

The module is **sans-IO**. No sockets, no timers, no greenlets are
touched here. Bytes flow in through ``feed()``, frames flow out through
``bytes_to_send()``; the caller pumps both sides.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING

from geventhttpclient.http2._parser import Session, session_client_new

if TYPE_CHECKING:
    # Stub-only alias (TypedDict union); the compiled module does not
    # export it at runtime.
    from geventhttpclient.http2._parser import Http2Event as RawHttp2Event

#: Stream-id 0 means "connection-level", not an actual stream.
CONNECTION_STREAM_ID = 0

#: Sentinel for :attr:`HTTP2Connection.MAX_CONCURRENT_STREAMS` until the
#: peer's SETTINGS frame has been received. RFC 9113 §6.5.2 lets us open
#: streams anyway — we just gate against a conservative default.
DEFAULT_MAX_CONCURRENT_STREAMS = 100

#: Client-side SETTINGS defaults. Empty on purpose: the C extension
#: already queues the initial SETTINGS frame on session creation
#: (advertising MAX_HEADER_LIST_SIZE), so no second frame is needed
#: here. Push is *not* disabled via ENABLE_PUSH=0: the C extension
#: refuses each pushed stream individually with RST_STREAM
#: REFUSED_STREAM (RFC 9113 §8.2), which keeps the connection alive
#: when a server pushes anyway. Callers may still pass explicit
#: ``local_settings`` overrides to :class:`HTTP2Connection`.
DEFAULT_LOCAL_SETTINGS: dict[int, int] = {}


class StreamLifecycle(IntEnum):
    """HTTP/2 stream state (RFC 9113 §5.1).

    The Python layer derives these from incoming HEADERS / DATA / RST
    events because the nghttp2 C API does not expose the stream state
    directly. Transitions:

    * ``IDLE`` ‖ HEADERS received/sent -> ``OPEN``
    * ``OPEN`` + local END_STREAM  -> ``HALF_CLOSED_LOCAL``
    * ``OPEN`` + remote END_STREAM -> ``HALF_CLOSED_REMOTE``
    * both half-closed events      -> ``CLOSED``
    * ``RST_STREAM`` received      -> ``CLOSED``
    * :class:`GoAwayReceived` whose ``last_stream_id >= self.stream_id``
      -> ``CLOSED``
    """

    IDLE = 0x0
    OPEN = 0x1
    HALF_CLOSED_LOCAL = 0x2
    HALF_CLOSED_REMOTE = 0x3
    CLOSED = 0x4


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class HeadersReceived:
    """Server pushed response (or server-pushed request) HEADERS.

    For client responses the stream is identified by ``stream_id``.
    ``end_stream=True`` on a response HEADERS frame means the server
    closed the stream with no body (RFC 9113 §8.1).
    """

    stream_id: int
    headers: tuple[tuple[str, str], ...]
    end_stream: bool

    @property
    def kind(self) -> str:
        return "headers"


@dataclass(slots=True, frozen=True)
class DataReceived:
    """A DATA frame chunk on an open stream.

    ``data`` may be empty (``b""``) when ``end_stream`` is TRUE and the
    peer sends a zero-length terminal DATA frame. Callers must tolerate
    that case explicitly (RFC 9113 §6.2).
    """

    stream_id: int
    data: bytes
    end_stream: bool

    @property
    def kind(self) -> str:
        return "data"


@dataclass(slots=True, frozen=True)
class StreamReset:
    """Server sent RST_STREAM on ``stream_id``.

    ``error_code`` is one of the :mod:`nghttp2` constants
    (NO_ERROR=0x0, PROTOCOL_ERROR=0x1, CANCEL=0x8, ...). The stream
    becomes :attr:`StreamLifecycle.CLOSED`.
    """

    stream_id: int
    error_code: int

    @property
    def kind(self) -> str:
        return "stream_reset"


@dataclass(slots=True, frozen=True)
class StreamClosed:
    """Stream closed cleanly (END_STREAM observed on both sides)."""

    stream_id: int
    error_code: int
    end_stream: bool

    @property
    def kind(self) -> str:
        return "stream_closed"


@dataclass(slots=True, frozen=True)
class SettingsReceived:
    """SETTINGS frame received (with ack)."""

    stream_id: int
    settings: dict[int, int]
    ack: bool

    @property
    def kind(self) -> str:
        return "settings"


@dataclass(slots=True, frozen=True)
class PingReceived:
    """PING frame received (or its ack)."""

    stream_id: int
    opaque_data: bytes
    ack: bool

    @property
    def kind(self) -> str:
        return "ping"


@dataclass(slots=True, frozen=True)
class GoAwayReceived:
    """GOAWAY frame received.

    ``last_stream_id`` is the highest stream-id the peer will process.
    Streams with ``stream_id > last_stream_id`` are *not* processed by
    the peer and must be retried on a fresh connection (RFC 9113
    §6.8). ``error_code`` follows the same convention as
    :class:`StreamReset`.
    """

    stream_id: int
    last_stream_id: int
    error_code: int
    debug_data: bytes

    @property
    def kind(self) -> str:
        return "goaway"


@dataclass(slots=True, frozen=True)
class WindowUpdateReceived:
    """WINDOW_UPDATE frame received.

    ``stream_id == 0`` is a connection-level update (RFC 9113
    §6.9.1); non-zero is a per-stream update that also feeds the
    connection-level window per §6.9.2.
    """

    stream_id: int
    delta: int

    @property
    def kind(self) -> str:
        return "window_update"


def _parse_status_code(headers: tuple[tuple[str, str], ...]) -> int | None:
    """Extract and parse the ``:status`` pseudo-header once.

    Returns None when the header is absent or not an integer (the
    caller treats both as "no final response status")."""
    status_str = next((v for n, v in headers if n == ":status"), None)
    if status_str is None:
        return None
    try:
        return int(status_str)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# HTTP/2 review-commentary events (RFC 9113 §8.1.1 / §8.1)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class InformationalResponseReceived:
    """1xx informational response (RFC 9113 §8.1.1).

    Examples: ``100 Continue``, ``103 Early Hints``. The server may
    emit zero or more of these before the final response. They do
    *not* end the stream — the final HEADERS frame carries the
    status code callers care about.
    """

    stream_id: int
    status_code: int
    headers: tuple[tuple[str, str], ...]

    @property
    def kind(self) -> str:
        return "informational"


@dataclass(slots=True, frozen=True)
class TrailerReceived:
    """Trailer HEADERS block (RFC 9113 §8.1).

    A HEADERS frame arriving *after* DATA frames on the same stream
    (or directly attached to ``END_STREAM=1`` for a zero-body response
    that the server still wants to label with trailers) is the
    trailer section. Trailers carry metadata that is only known after
    the body has been generated (``Digest``, ``Server-Timing``,
    etc.). They are merged into the response trailer dict, **not**
    the headers dict, so callers see a clean separation.
    """

    stream_id: int
    headers: tuple[tuple[str, str], ...]

    @property
    def kind(self) -> str:
        return "trailer"


Http2Event = (
    HeadersReceived
    | DataReceived
    | StreamReset
    | StreamClosed
    | SettingsReceived
    | PingReceived
    | GoAwayReceived
    | WindowUpdateReceived
    | InformationalResponseReceived
    | TrailerReceived
)


# ---------------------------------------------------------------------------
# Stream state
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class StreamState:
    """Per-stream aggregate owned by :class:`HTTP2Connection`.

    The :class:`HTTP2Connection` is single-owner — all access happens on
    the pump greenlet (or any thread, but not concurrently). External
    callers should treat the returned reference as a snapshot.
    """

    stream_id: int
    state: StreamLifecycle = StreamLifecycle.IDLE
    response_status_code: int | None = None
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    response_body_parts: list[bytes] = field(default_factory=list)
    data_received: bool = False
    reset_error_code: int | None = None
    trailer_headers: tuple[tuple[str, str], ...] | None = None

    def is_closed(self) -> bool:
        return self.state == StreamLifecycle.CLOSED

    def is_open_for_sending(self) -> bool:
        """True if we may still submit DATA frames on this stream."""
        return self.state in (StreamLifecycle.OPEN, StreamLifecycle.HALF_CLOSED_REMOTE)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------


class HTTP2Connection:
    """Sans-IO HTTP/2 client connection.

    Single owner — one pump greenlet in Phase 4. ``feed()`` is not
    thread-safe against ``submit_*()``; serialise calls in the same
    thread/greenlet.
    """

    def __init__(
        self,
        *,
        local_settings: dict[int, int] | None = None,
        session: Session | None = None,
    ) -> None:
        self._session: Session = session if session is not None else session_client_new()
        # Outbound frames queue, in arrival order. submit_*() may push
        # multiple frames (SETTINGS + ACK + WINDOW_UPDATE auto-paths);
        # bytes_to_send() drains them.
        self._outbound: list[bytes] = []
        # Per-stream aggregates. ``self._streams[stream_id]`` is created
        # on first HEADERS event for that id.
        self._streams: dict[int, StreamState] = {}
        # Peer's MAX_CONCURRENT_STREAMS (RFC 9113 §6.5.2). We gate new
        # submit_*() calls against this. Updated when the peer sends
        # SETTINGS. Initialise to a conservative default so that the
        # first request before the server's SETTINGS frame still works.
        self._remote_max_concurrent_streams = DEFAULT_MAX_CONCURRENT_STREAMS
        # Highest stream-id the peer will process (per last GOAWAY).
        # Streams with higher ids must be retried on a new connection.
        # ``2**31 - 1`` means "no GOAWAY received yet".
        self._last_accepted_stream_id = 2**31 - 1
        # The C extension queues an empty initial-SETTINGS frame on
        # session creation so the connection preface is on the wire.
        # We only submit our own SETTINGS when the caller passed
        # overrides; the canonical defaults live at the C layer (see
        # DEFAULT_LOCAL_SETTINGS).
        merged: dict[int, int] = dict(DEFAULT_LOCAL_SETTINGS)
        if local_settings:
            merged.update(local_settings)
        if merged:
            frames = self._session.submit_settings(merged)
            if frames:
                self._outbound.append(frames)
        else:
            # Drain the C-queued connection preface + initial SETTINGS
            # (a zero-length recv produces no events, only outbound).
            _, frames = self._session.recv(b"")
            if frames:
                self._outbound.append(frames)
        # Track how many streams we have opened against the peer budget.
        self._open_streams: int = 0

    # -- Introspection -----------------------------------------------------

    @property
    def session(self) -> Session:
        """Raw nghttp2 session. Exposed for power users; Phase 4 wraps
        it."""
        return self._session

    @property
    def next_stream_id(self) -> int:
        return self._session.next_stream_id()

    @property
    def local_settings(self) -> dict[int, int]:
        return self._session.get_local_settings()

    @property
    def remote_settings(self) -> dict[int, int]:
        return self._session.get_remote_settings()

    @property
    def streams(self) -> dict[int, StreamState]:
        """Read-only view of the streams we have seen. Mutating the
        returned dict is undefined behaviour."""
        return self._streams

    @property
    def last_accepted_stream_id(self) -> int:
        """Highest stream-id the peer will accept (RFC 9113 §6.8)."""
        return self._last_accepted_stream_id

    @property
    def remote_max_concurrent_streams(self) -> int:
        return self._remote_max_concurrent_streams

    @property
    def open_streams(self) -> int:
        """Number of streams currently held against the
        ``MAX_CONCURRENT_STREAMS`` budget.

        Note: includes streams that are half-closed or fully closed but
        not yet pruned from the dict. Use :meth:`prune_closed_streams`
        if memory growth is a concern.
        """
        return self._open_streams

    def get_stream(self, stream_id: int) -> StreamState | None:
        return self._streams.get(stream_id)

    # -- Outbound ----------------------------------------------------------

    def bytes_to_send(self) -> bytes:
        """Drain all queued outbound frames as a single ``bytes``.

        ``bytes_to_send()`` does *not* peek: it empties the queue.
        Frames are returned in submission order; ACKs and
        PING-responses arrive in the order nghttp2 emitted them.
        """
        if not self._outbound:
            return b""
        out = b"".join(self._outbound)
        self._outbound.clear()
        return out

    def has_outbound(self) -> bool:
        return bool(self._outbound)

    # -- Submit ------------------------------------------------------------

    def submit_request(
        self,
        method: str,
        path: str,
        authority: str,
        headers: Iterable[tuple[str, str]] | None = None,
        *,
        scheme: str = "https",
        body: bytes | None = None,
    ) -> int:
        """Submit a request HEADERS frame. Returns the assigned stream_id.

        Pseudo-headers (:method, :scheme, :path, :authority) are inserted
        automatically and override anything passed in ``headers``.

        When ``body`` is given, the bytes are submitted as a single
        DATA frame with END_STREAM right after the HEADERS frame. The
        local side moves to HALF_CLOSED_LOCAL through :meth:`submit_data`
        bookkeeping so the state machine reflects the close.

        For multi-chunk uploads, pass ``body=None`` and call
        :meth:`submit_data` directly; ``submit_data`` still owns the
        END_STREAM decision via its ``end_stream`` flag.
        """
        if self._session.next_stream_id() > self._last_accepted_stream_id:
            raise BlockingIOError(
                "peer sent GOAWAY; this stream id would not be processed",
                self._session.next_stream_id(),
            )
        if self._open_streams >= self._remote_max_concurrent_streams:
            raise BlockingIOError(
                f"peer MAX_CONCURRENT_STREAMS={self._remote_max_concurrent_streams} reached",
                self._open_streams,
            )
        # Build the header list. We lowercase pseudo-headers and forbid
        # user-supplied duplicates to avoid nghttp2 validation errors.
        merged: list[tuple[str, str]] = [
            (":method", method),
            (":scheme", scheme),
            (":path", path),
            (":authority", authority),
        ]
        if headers:
            for name, value in headers:
                if name.startswith(":"):
                    # Pseudo-headers are ours to populate. Silently
                    # ignore user-supplied ones; nghttp2 would error
                    # out otherwise.
                    continue
                merged.append((name, value))

        stream_id, frames = self._session.submit_request(merged, with_body=body is not None)
        if frames:
            self._outbound.append(frames)
        if stream_id in self._streams:
            # Submitting twice for the same stream id should be
            # impossible (stream ids are monotonic) but guard anyway.
            raise RuntimeError(f"stream {stream_id} already exists")
        # Local-side stream state. With ``body=None`` we END_STREAM on
        # the HEADERS frame and the local side is therefore immediately
        # HALF_CLOSED_LOCAL; otherwise it stays OPEN until we submit a
        # final DATA frame (see ``submit_data``).
        initial_state = (StreamLifecycle.HALF_CLOSED_LOCAL
                         if body is None else StreamLifecycle.OPEN)
        state = StreamState(stream_id=stream_id, state=initial_state)
        self._streams[stream_id] = state
        self._open_streams += 1

        # Body bytes are submitted as a single DATA frame immediately
        # after the HEADERS frame; ``submit_data`` advances the state
        # machine from OPEN to HALF_CLOSED_LOCAL via end_stream=True.
        # An empty ``body`` is the convention for "no body, END_STREAM on
        # HEADERS", which is what ``with_body=False`` produces; we treat
        # it the same as ``body=None``.
        if body:
            self.submit_data(stream_id, body, end_stream=True)
        return stream_id

    def submit_data(
        self,
        stream_id: int,
        data: bytes,
        end_stream: bool = False,
    ) -> None:
        """Submit a DATA frame on an open stream.

        ``data`` must be a ``bytes``-like object (the C extension
        accepts ``bytes``/``bytearray``/``memoryview``). Empty ``data``
        with ``end_stream=True`` sends a zero-length terminal DATA
        frame (RFC 9113 §6.2).

        When ``end_stream=True`` we advance the local side of the
        state-machine: ``OPEN → HALF_CLOSED_LOCAL`` (or
        ``HALF_CLOSED_REMOTE → CLOSED``). Counter decrements on
        ``CLOSED``.
        """
        frames = self._session.submit_data(stream_id, bytes(data), end_stream)
        if frames:
            self._outbound.append(frames)
        if end_stream:
            state = self._streams.get(stream_id)
            if state is not None:
                if state.state == StreamLifecycle.HALF_CLOSED_REMOTE:
                    state.state = StreamLifecycle.CLOSED
                    self._open_streams = max(0, self._open_streams - 1)
                elif state.state == StreamLifecycle.OPEN:
                    state.state = StreamLifecycle.HALF_CLOSED_LOCAL

    def submit_trailers(self, stream_id: int, trailers: Iterable[tuple[str, str]]) -> None:
        """Submit trailer HEADERS on a half-closed-remote stream.

        The trailer frame itself ends the stream (RFC 9113 §8.1: every
        HEADERS frame has END_STREAM implicit when there is no body).
        """
        frames = self._session.submit_trailer(stream_id, list(trailers))
        if frames:
            self._outbound.append(frames)

    def submit_rst_stream(self, stream_id: int, error_code: int = 0) -> None:
        frames = self._session.submit_rst_stream(stream_id, error_code)
        if frames:
            self._outbound.append(frames)

    def submit_window_update(self, stream_id: int, increment: int) -> None:
        frames = self._session.submit_window_update(stream_id, increment)
        if frames:
            self._outbound.append(frames)

    def submit_settings(self, settings: dict[int, int]) -> None:
        frames = self._session.submit_settings(dict(settings))
        if frames:
            self._outbound.append(frames)

    def submit_priority_update(self, stream_id: int, field_value: bytes) -> None:
        frames = self._session.submit_priority_update(stream_id, bytes(field_value))
        if frames:
            self._outbound.append(frames)

    def submit_goaway(
        self,
        last_stream_id: int,
        error_code: int = 0,
        debug_data: bytes = b"",
    ) -> None:
        frames = self._session.submit_goaway(last_stream_id, error_code, bytes(debug_data))
        if frames:
            self._outbound.append(frames)

    # -- Inbound -----------------------------------------------------------

    def feed(self, data: bytes | bytearray | memoryview) -> list[Http2Event]:
        """Push inbound bytes into the nghttp2 session.

        Returns the events this batch produced, in arrival order. The
        caller is expected to follow up with :meth:`bytes_to_send` to
        obtain the ACK / response / PING-reply frames nghttp2 emitted
        during the recv() pass.

        The C extension emits ``stream_closed`` from a single source
        (``on_stream_close``, invoked by nghttp2 exactly once per
        stream); a DATA-frame END_STREAM surfaces as
        :class:`DataReceived` with ``end_stream=True`` instead, so no
        deduplication is needed here.
        """
        events, outbound = self._session.recv(bytes(data))
        if outbound:
            self._outbound.append(outbound)
        return [
            event
            for raw in events
            if (event := self._convert_event(raw)) is not None
        ]

    # -- Helpers -----------------------------------------------------------

    def _convert_event(self, raw: "RawHttp2Event") -> Http2Event | None:
        # ``_kind`` discriminates the raw TypedDict union; the type
        # checker narrows ``raw`` in each branch.
        if raw["_kind"] == "headers":
            stream_id = raw["stream_id"]
            end_stream = raw["end_stream"]
            all_headers = tuple(raw["headers"])
            # RFC 9113 §8.1.1: 1xx informational responses (100, 103,
            # ...) precede the final response. They are *not* stored in
            # ``StreamState.response_status_code`` and are emitted as
            # ``InformationalResponseReceived`` so callers can inspect
            # early hints without confusing them for the real answer.
            status_code = _parse_status_code(all_headers)
            state = self._streams.get(stream_id)
            if (
                status_code is not None
                and 100 <= status_code < 200
            ):
                headers = tuple(h for h in all_headers if h[0] != ":status")
                return InformationalResponseReceived(
                    stream_id, status_code, headers,
                )
            # Trailer detection (RFC 9113 §8.1): a HEADERS frame on a
            # stream whose final response was already delivered is the
            # trailer section. We emit a ``TrailerReceived`` event and
            # leave the original ``response_headers`` untouched.
            if state is not None and state.response_status_code is not None:
                trailers = tuple(h for h in all_headers if h[0] != ":status")
                state.trailer_headers = trailers
                if end_stream:
                    self._mark_remote_closed(state)
                return TrailerReceived(stream_id, trailers)
            if state is None and status_code is None:
                # RFC 9113 §8.1: the first HEADERS frame of a response
                # must carry :status. A HEADERS block without one on a
                # stream we have no state for is malformed (e.g. a
                # stray block on a stream we never opened) -- drop it
                # instead of creating bogus stream state that would
                # also break trailer detection on follow-up blocks.
                return None
            self._update_stream_state_on_headers(
                stream_id, all_headers, end_stream, status_code)
            # Surface to the user: the :status pseudo-header has been
            # captured into ``StreamState.response_status_code`` and is
            # filtered out of the public headers tuple. See also the
            # same filter in ``_update_stream_state_on_headers``.
            headers = tuple(h for h in all_headers if h[0] != ":status")
            return HeadersReceived(stream_id, headers, end_stream)
        if raw["_kind"] == "data":
            stream_id = raw["stream_id"]
            end_stream = raw["end_stream"]
            self._update_stream_state_on_data(stream_id, end_stream)
            return DataReceived(stream_id, raw["data"], end_stream)
        if raw["_kind"] == "stream_reset":
            stream_id = raw["stream_id"]
            error_code = raw["error_code"]
            self._update_stream_state_on_reset(stream_id, error_code)
            return StreamReset(stream_id, error_code)
        if raw["_kind"] == "stream_closed":
            stream_id = raw["stream_id"]
            error_code = raw["error_code"]
            self._update_stream_state_on_close(stream_id, error_code)
            return StreamClosed(stream_id, error_code, raw["end_stream"])
        if raw["_kind"] == "settings":
            stream_id = raw["stream_id"]
            settings = raw["settings"]
            self._update_settings(settings, raw["ack"])
            return SettingsReceived(stream_id, settings, raw["ack"])
        if raw["_kind"] == "ping":
            return PingReceived(
                raw["stream_id"], raw["opaque_data"], raw["ack"])
        if raw["_kind"] == "goaway":
            stream_id = raw["stream_id"]
            last = raw["last_stream_id"]
            self._update_last_accepted(last)
            return GoAwayReceived(
                stream_id, last, raw["error_code"], raw["debug_data"])
        if raw["_kind"] == "window_update":
            return WindowUpdateReceived(raw["stream_id"], raw["increment"])
        raise RuntimeError(f"unknown HTTP/2 event kind: {raw['_kind']!r}")

    def _update_settings(self, settings: dict[int, int], ack: bool) -> None:
        # ACKs are just for our SETTINGS; only update our model when the
        # peer pushes new settings.
        if ack:
            return
        # RFC 9113 §6.5.2: setting ids are 16-bit unsigned.
        max_concurrent_streams_id = 0x3
        if max_concurrent_streams_id in settings:
            self._remote_max_concurrent_streams = int(settings[max_concurrent_streams_id])

    def _update_last_accepted(self, last_stream_id: int) -> None:
        # GOAWAY may be sent twice (graceful shutdown). The second
        # GOAWAY's last_stream_id is the binding one.
        self._last_accepted_stream_id = min(self._last_accepted_stream_id, last_stream_id)

    def _update_stream_state_on_headers(
        self,
        stream_id: int,
        headers: tuple[tuple[str, str], ...],
        end_stream: bool,
        status_code: int | None,
    ) -> None:
        state = self._streams.get(stream_id)
        if state is None:
            state = StreamState(stream_id=stream_id)
            self._streams[stream_id] = state
        # Was-idle -> OPEN on first HEADERS (response from server).
        if state.state == StreamLifecycle.IDLE:
            state.state = StreamLifecycle.OPEN
        # Capture :status (parsed once by the caller) for response
        # aggregation. The pseudo-header itself is *not* stored in
        # ``response_headers``.
        state.response_status_code = status_code
        state.response_headers.extend(
            (name, value) for name, value in headers if name != ":status"
        )
        if end_stream:
            self._mark_remote_closed(state)

    def _update_stream_state_on_data(self, stream_id: int, end_stream: bool) -> None:
        state = self._streams.get(stream_id)
        if state is None:
            return
        state.data_received = True
        if end_stream:
            self._mark_remote_closed(state)

    def _update_stream_state_on_reset(self, stream_id: int, error_code: int) -> None:
        state = self._streams.get(stream_id)
        if state is None:
            return
        state.reset_error_code = error_code
        state.state = StreamLifecycle.CLOSED
        self._open_streams = max(0, self._open_streams - 1)

    def _update_stream_state_on_close(self, stream_id: int, error_code: int) -> None:
        """Process the terminal ``stream_closed`` event. The C side
        emits it exactly once per stream (from
        ``on_stream_close_callback``); a DATA-frame END_STREAM is a
        separate ``data`` event, so no duplicate suppression is
        needed. RST_STREAM already surfaced as ``stream_reset`` and
        closed the state -- the follow-up ``stream_closed`` for the
        same stream is then a no-op here."""
        state = self._streams.get(stream_id)
        if state is None:
            return
        # nghttp2 fires ``on_stream_close`` *after* RST_STREAM was
        # surfaced as a ``stream_reset`` event; the state was already
        # CLOSED and the counter already decremented in that path.
        if state.state == StreamLifecycle.CLOSED:
            return
        state.state = StreamLifecycle.CLOSED
        if error_code != 0 and state.reset_error_code is None:
            state.reset_error_code = error_code
        self._open_streams = max(0, self._open_streams - 1)

    def _mark_remote_closed(self, state: StreamState) -> None:
        if state.state == StreamLifecycle.HALF_CLOSED_LOCAL:
            state.state = StreamLifecycle.CLOSED
            self._open_streams = max(0, self._open_streams - 1)
        elif state.state == StreamLifecycle.OPEN:
            state.state = StreamLifecycle.HALF_CLOSED_REMOTE
        # IDLE and HALF_CLOSED_REMOTE: nothing more to do.

    def prune_closed_streams(self) -> Iterator[int]:
        """Yield and drop streams in :attr:`StreamLifecycle.CLOSED`.

        Useful after a pump-drain to free per-stream state.
        """
        closed_ids = [sid for sid, s in self._streams.items() if s.is_closed()]
        for sid in closed_ids:
            del self._streams[sid]
            yield sid


__all__ = [
    "CONNECTION_STREAM_ID",
    "DEFAULT_MAX_CONCURRENT_STREAMS",
    "DataReceived",
    "GoAwayReceived",
    "HTTP2Connection",
    "HeadersReceived",
    "Http2Event",
    "PingReceived",
    "SettingsReceived",
    "StreamClosed",
    "StreamLifecycle",
    "StreamReset",
    "StreamState",
    "WindowUpdateReceived",
]
