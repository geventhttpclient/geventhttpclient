"""Type stubs for the geventhttpclient.http2._parser C extension.

Sans-IO HTTP/2 session backed by the vendored nghttp2 library. All
``submit_*`` methods return the outbound frame bytes to send; ``recv``
consumes inbound bytes and returns ``(events, outbound_frames)``.

The raw events are plain dicts at runtime; the TypedDicts below (with
``_kind`` as discriminator) let type checkers narrow them.
"""

from typing import Literal, TypeAlias, TypedDict


class HeadersEvent(TypedDict):
    _kind: Literal["headers"]
    stream_id: int
    headers: list[tuple[str, str]]
    end_stream: bool


class DataEvent(TypedDict):
    _kind: Literal["data"]
    stream_id: int
    data: bytes
    end_stream: bool


class StreamResetEvent(TypedDict):
    _kind: Literal["stream_reset"]
    stream_id: int
    error_code: int


class StreamClosedEvent(TypedDict):
    _kind: Literal["stream_closed"]
    stream_id: int
    error_code: int
    end_stream: bool


class SettingsEvent(TypedDict):
    _kind: Literal["settings"]
    stream_id: int
    settings: dict[int, int]
    ack: bool


class PingEvent(TypedDict):
    _kind: Literal["ping"]
    stream_id: int
    opaque_data: bytes
    ack: bool


class GoAwayEvent(TypedDict):
    _kind: Literal["goaway"]
    stream_id: int
    last_stream_id: int
    error_code: int
    debug_data: bytes


class WindowUpdateEvent(TypedDict):
    _kind: Literal["window_update"]
    stream_id: int
    increment: int


#: Raw event dicts as produced by ``Session.recv`` (discriminated by
#: the ``_kind`` field).
Http2Event: TypeAlias = (
    HeadersEvent
    | DataEvent
    | StreamResetEvent
    | StreamClosedEvent
    | SettingsEvent
    | PingEvent
    | GoAwayEvent
    | WindowUpdateEvent
)


class Session:
    """Sans-IO HTTP/2 client session (one per connection)."""

    def recv(
        self, data: bytes | bytearray | memoryview
    ) -> tuple[list[Http2Event], bytes]: ...

    def submit_request(
        self, headers: list[tuple[str, str]], with_body: bool = False
    ) -> tuple[int, bytes]: ...

    def submit_data(
        self, stream_id: int, data: bytes, end_stream: bool
    ) -> bytes: ...

    def submit_response(
        self, stream_id: int, headers: list[tuple[str, str]], with_body: bool = False
    ) -> bytes:
        """Server-side only: submit response HEADERS (:status required)."""

    def submit_headers(
        self, stream_id: int, headers: list[tuple[str, str]], end_stream: bool
    ) -> bytes: ...

    def submit_trailer(
        self, stream_id: int, headers: list[tuple[str, str]]
    ) -> bytes: ...

    def submit_settings(self, settings: dict[int, int]) -> bytes: ...

    def submit_ping(self, opaque_data: bytes) -> bytes: ...

    def submit_goaway(
        self, last_stream_id: int, error_code: int, debug_data: bytes = b""
    ) -> bytes: ...

    def submit_window_update(self, stream_id: int, increment: int) -> bytes: ...

    def submit_rst_stream(self, stream_id: int, error_code: int) -> bytes: ...

    def submit_priority_update(self, stream_id: int, field_value: bytes) -> bytes: ...

    def submit_shutdown_notice(self) -> bytes: ...

    def next_stream_id(self) -> int: ...

    def get_stream_remote_window_size(self, stream_id: int) -> int | None:
        """Bytes we may still send on ``stream_id`` (None if unknown).

        Use for upload backpressure: pace ``submit_data`` calls so the
        collected body stays bounded when the peer's window is small."""

    def get_stream_local_window_size(self, stream_id: int) -> int | None:
        """Bytes the peer may still send on ``stream_id`` (None if unknown)."""

    def get_remote_window_size(self) -> int:
        """Connection-level bytes we may still send."""

    def get_local_window_size(self) -> int:
        """Connection-level bytes the peer may still send."""

    def get_local_settings(self) -> dict[int, int]: ...

    def get_remote_settings(self) -> dict[int, int]: ...


def session_client_new() -> Session: ...


def session_server_new() -> Session:
    """Create a server-side session (used by round-trip tests)."""
