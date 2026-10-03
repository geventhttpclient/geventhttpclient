"""Type stubs for the geventhttpclient._http2_parser C extension.

Sans-IO HTTP/2 session backed by the vendored nghttp2 library. All
``submit_*`` methods return the outbound frame bytes to send; ``recv``
consumes inbound bytes and returns ``(events, outbound_frames)``.
"""

from typing import Any, TypeAlias

#: Event dict kind values as produced by ``Session.recv``.
HeadersEvent: TypeAlias = dict[str, Any]      # _kind="headers": stream_id, headers, end_stream
DataEvent: TypeAlias = dict[str, Any]         # _kind="data": stream_id, data, end_stream
StreamResetEvent: TypeAlias = dict[str, Any]  # _kind="stream_reset": stream_id, error_code
StreamClosedEvent: TypeAlias = dict[str, Any]  # _kind="stream_closed": stream_id, error_code, end_stream
SettingsEvent: TypeAlias = dict[str, Any]     # _kind="settings": stream_id, settings, ack
PingEvent: TypeAlias = dict[str, Any]         # _kind="ping": stream_id, opaque_data, ack
GoAwayEvent: TypeAlias = dict[str, Any]       # _kind="goaway": stream_id, last_stream_id, error_code, debug_data
WindowUpdateEvent: TypeAlias = dict[str, Any]  # _kind="window_update": stream_id, increment

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

    def get_local_settings(self) -> dict[int, int]: ...

    def get_remote_settings(self) -> dict[int, int]: ...


def session_client_new() -> Session: ...


def session_server_new() -> Session:
    """Create a server-side session (used by round-trip tests)."""
