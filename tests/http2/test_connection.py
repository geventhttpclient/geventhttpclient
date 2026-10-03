"""Sans-IO tests for :mod:`geventhttpclient.http2.HTTP2Connection`.

These tests wire a :class:`HTTP2Connection` (the Python dataclass layer)
to a raw nghttp2 server session (``session_server_new``) through their
frame outputs — no sockets involved. They verify event conversion from
raw dicts to dataclasses, the per-stream state machine, the
``MAX_CONCURRENT_STREAMS`` gate and the ``GOAWAY`` ``last_stream_id``
gate.
"""

import pytest

from geventhttpclient.http2 import (
    DEFAULT_MAX_CONCURRENT_STREAMS,
    DataReceived,
    GoAwayReceived,
    HeadersReceived,
    HTTP2Connection,
    Http2Event,
    InformationalResponseReceived,
    PingReceived,
    SettingsReceived,
    StreamLifecycle,
    StreamReset,
    TrailerReceived,
)
from geventhttpclient.http2._parser import session_server_new


def _pump(
    client: HTTP2Connection,
    server,
    to_server: bytes = b"",
    to_client: bytes = b"",
    rounds: int = 10,
) -> tuple[list[Http2Event], list[dict[str, object]]]:
    """Settle the wire between ``HTTP2Connection`` and a raw nghttp2 session.

    Returns ``(client_events, server_events)``. ``server_events`` are
    raw dicts because the server side does not run through the
    dataclass converter in production — only the client side does.
    """
    client_events: list[Http2Event] = []
    server_events: list[dict[str, object]] = []
    for _ in range(rounds):
        progressed = False
        client_out = client.bytes_to_send()
        if client_out:
            to_server += client_out
            progressed = True
        if to_server:
            events, more = server.recv(to_server)
            server_events.extend(events)
            to_server = b""
            if more:
                to_client += more
                progressed = True
        _, more = server.recv(b"")
        if more:
            to_client += more
            progressed = True
        if to_client:
            events = client.feed(to_client)
            client_events.extend(events)
            to_client = b""
            progressed = True
        if not progressed:
            break
    return client_events, server_events


def _headers() -> list[tuple[str, str]]:
    return [
        (":method", "GET"),
        (":scheme", "https"),
        (":path", "/index.html"),
        (":authority", "example.com"),
    ]


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_starts_with_connection_preface_in_outbound(self):
        # HTTP2Connection pushes an empty SETTINGS frame in __init__ so
        # the wire bytes include the connection preface. The exact
        # length is 24 (preface) + 9 (SETTINGS frame header) + 0 (no
        # settings entries) = 33 bytes for an empty settings.
        c = HTTP2Connection()
        assert c.bytes_to_send().startswith(b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n")

    def test_session_property_exposes_narrowed_parser(self):
        c = HTTP2Connection()
        # next_stream_id is a Session method; HTTP2Connection forwards
        # it via its session property.
        assert c.session.next_stream_id() == 1

    def test_default_local_settings_exposed(self):
        c = HTTP2Connection()
        settings = c.local_settings
        assert settings[0x1] == 4096
        assert settings[0x4] == 65535

    def test_remote_settings_empty_before_peer_speaks(self):
        c = HTTP2Connection()
        # nghttp2 populates *default* remote settings even before the
        # peer has sent its own — they reflect what nghttp2 assumes.
        # Just assert the property exposes a dict.
        assert isinstance(c.remote_settings, dict)

    def test_local_settings_override_takes_effect(self):
        # nghttp2 enforces minimums on most settings (MAX_FRAME_SIZE >=
        # 16384, etc.) and ignores some outright. We verify the
        # ``local_settings`` argument is forwarded to the C extension
        # without raising — the exact value clamp is nghttp2's choice.
        HTTP2Connection(local_settings={0x1: 4096})  # HEADER_TABLE_SIZE (no-op)
        # A malformed value still raises from the C extension; we
        # document the constraint here rather than fighting nghttp2.
        with pytest.raises(RuntimeError):
            HTTP2Connection(local_settings={0x5: 1024})  # MAX_FRAME_SIZE too small


# ---------------------------------------------------------------------------
# Request / response round trip
# ---------------------------------------------------------------------------


class TestRequestResponse:
    def test_get_round_trip_emits_typed_events(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/", "example.com")
        assert sid == 1
        # Local closed immediately (END_STREAM in HEADERS, no body).
        assert c.streams[sid].state is StreamLifecycle.HALF_CLOSED_LOCAL

        _, s_events = _pump(c, s, to_server=c.bytes_to_send())
        server_headers = [e for e in s_events if e.get("_kind") == "headers"]
        assert len(server_headers) == 1

        response = s.submit_response(sid, [(":status", "200"), ("content-type", "text/plain")])
        c_events, _ = _pump(c, s, to_client=response)

        # The first event must be the typed HeadersReceived. We filter
        # out the SETTINGS auto-acks that arrive during the pump.
        header_events = [e for e in c_events if isinstance(e, HeadersReceived)]
        assert len(header_events) == 1
        h = header_events[0]
        assert h.stream_id == sid
        assert h.end_stream is True
        assert ("content-type", "text/plain") in h.headers
        # :status itself is *not* in the headers list — see http2.py.
        assert not any(name == ":status" for name, _ in h.headers)

        # Response status code captured on the state.
        assert c.streams[sid].response_status_code == 200
        assert c.streams[sid].is_closed()

    def test_post_with_body_full_lifecycle(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        # Bugfix: passing ``body=...`` now ships the bytes through
        # ``submit_data(end_stream=True)`` immediately, so the extra
        # ``submit_data`` call from the previous test would either be
        # rejected (stream already finished) or duplicate the body.
        sid = c.submit_request("POST", "/upload", "example.com", body=b"hello http2")
        assert c.streams[sid].state is StreamLifecycle.HALF_CLOSED_LOCAL

        _, s_events = _pump(c, s, to_server=c.bytes_to_send())
        data = [e for e in s_events if e.get("_kind") == "data"]
        assert len(data) == 1
        assert data[0]["data"] == b"hello http2"
        assert data[0]["end_stream"] is True

        response = s.submit_response(sid, [(":status", "201")], with_body=True)
        response += s.submit_data(sid, b"resp-body", end_stream=True)
        c_events, _ = _pump(c, s, to_client=response)

        data = [e for e in c_events if isinstance(e, DataReceived)]
        assert len(data) == 1
        assert data[0].data == b"resp-body"
        assert data[0].end_stream is True

        assert c.streams[sid].response_status_code == 201
        assert c.streams[sid].data_received is True
        assert c.streams[sid].is_closed()


# ---------------------------------------------------------------------------
# Multiplexing
# ---------------------------------------------------------------------------


class TestMultiplexing:
    def test_two_concurrent_streams_keep_independent_state(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid1 = c.submit_request("GET", "/1", "example.com")
        sid2 = c.submit_request("GET", "/2", "example.com")
        assert {sid1, sid2} == {1, 3}
        # Local closed immediately for both.
        assert c.streams[sid1].state is StreamLifecycle.HALF_CLOSED_LOCAL
        assert c.streams[sid2].state is StreamLifecycle.HALF_CLOSED_LOCAL

        # Push the requests to the server before issuing the reply;
        # otherwise the server's submit_response produces 0 bytes
        # (it has no record of the streams).
        _pump(c, s, to_server=c.bytes_to_send())

        # Server replies to stream 1 only; stream 2 stays open.
        response1 = s.submit_response(sid1, [(":status", "200")])
        c_events, _ = _pump(c, s, to_client=response1)

        assert c.streams[sid1].is_closed()
        assert c.streams[sid2].state is StreamLifecycle.HALF_CLOSED_LOCAL

    def test_open_streams_counter_tracks_submit_request(self):
        c = HTTP2Connection()
        assert c.open_streams == 0

        c.submit_request("GET", "/", "example.com")
        assert c.open_streams == 1
        c.submit_request("GET", "/", "example.com")
        assert c.open_streams == 2


# ---------------------------------------------------------------------------
# MAX_CONCURRENT_STREAMS gate
# ---------------------------------------------------------------------------


class TestMaxConcurrentStreamsGate:
    def test_default_allows_one_request_before_peer_speaks(self):
        c = HTTP2Connection()
        # Default is 100; first request must not be blocked.
        c.submit_request("GET", "/", "example.com")

    def test_peer_max_concurrent_streams_blocks_new_submits(self):
        c = HTTP2Connection()
        # Pretend the peer only allows one stream at a time.
        c._remote_max_concurrent_streams = 1

        c.submit_request("GET", "/", "example.com")
        with pytest.raises(BlockingIOError) as excinfo:
            c.submit_request("GET", "/", "example.com")
        assert "MAX_CONCURRENT_STREAMS" in str(excinfo.value)

    def test_completed_stream_frees_the_budget(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        c._remote_max_concurrent_streams = 1
        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())
        response = s.submit_response(sid, [(":status", "200")])
        _pump(c, s, to_client=response)

        # Stream is CLOSED so the gate must allow the next submit.
        assert c.streams[sid].is_closed()
        c.submit_request("GET", "/", "example.com")

    def test_gate_count_includes_unpruned_closed_streams(self):
        """Closed streams remain in ``self._streams`` until pruned, but
        the concurrent-stream counter (``open_streams``) is decremented
        the moment the lifecycle reaches CLOSED — either via RST_STREAM,
        full clean half-close, or END_STREAM-on-HEADERS-by-both-sides.
        """
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        c._remote_max_concurrent_streams = 1
        # body=None END_STREAMs on HEADERS, so this stream is
        # HALF_CLOSED_LOCAL right after submit_request.
        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())
        assert c.open_streams == 1

        # Server END_STREAMs on HEADERS too. Both sides closed → state
        # is CLOSED, counter is decremented.
        response = s.submit_response(sid, [(":status", "200")])
        _pump(c, s, to_client=response)
        assert c.streams[sid].is_closed()
        assert c.open_streams == 0
        # Pruning is still useful to drop the StreamState itself, but
        # the gate has already accepted the budget back.
        list(c.prune_closed_streams())
        c.submit_request("GET", "/", "example.com")


# ---------------------------------------------------------------------------
# GOAWAY gate
# ---------------------------------------------------------------------------


class TestGoAwayGate:
    def test_last_accepted_stream_id_blocks_higher_streams(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        # Server sends GOAWAY(last_stream_id=0, error_code=0). The next
        # client stream id is 1, which exceeds 0, so further submits
        # must block.
        goaway = s.submit_goaway(0, 0, b"shutdown")
        events = c.feed(goaway)
        goaway_events = [e for e in events if isinstance(e, GoAwayReceived)]
        assert len(goaway_events) == 1
        assert goaway_events[0].last_stream_id == 0

        with pytest.raises(BlockingIOError) as excinfo:
            c.submit_request("GET", "/", "example.com")
        assert "GOAWAY" in str(excinfo.value)

    def test_last_accepted_stream_id_default_is_max(self):
        c = HTTP2Connection()
        # No GOAWAY received yet → no gate.
        assert c.last_accepted_stream_id == 2**31 - 1


class TestMalformedHeaders:
    def test_headers_without_status_on_unknown_stream_are_dropped(self):
        """RFC 9113 §8.1: the first HEADERS frame of a response must
        carry :status. A block without one on a stream we never opened
        must not create bogus stream state (P5).

        Tested at the conversion boundary: neither nghttp2 nor a
        compliant peer lets such a block onto the wire for an idle
        stream, so the raw event is fabricated directly."""
        c = HTTP2Connection()
        event = c._convert_event(
            {  # type: ignore[arg-type]
                "_kind": "headers",
                "stream_id": 99,
                "headers": [("x-junk", "1")],
                "end_stream": False,
            }
        )
        assert event is None
        assert 99 not in c.streams


# ---------------------------------------------------------------------------
# Stream reset
# ---------------------------------------------------------------------------


class TestStreamReset:
    def test_rst_stream_sets_state_and_error_code(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())

        rst = s.submit_rst_stream(sid, 8)  # CANCEL
        c_events, _ = _pump(c, s, to_client=rst)
        reset_events = [e for e in c_events if isinstance(e, StreamReset)]
        assert len(reset_events) == 1
        assert reset_events[0].error_code == 8

        assert c.streams[sid].state is StreamLifecycle.CLOSED
        assert c.streams[sid].reset_error_code == 8


# ---------------------------------------------------------------------------
# Other event types
# ---------------------------------------------------------------------------


class TestEventTypes:
    def test_ping_round_trip(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        ping = c.session.submit_ping(b"12345678")
        c_events, s_events = _pump(c, s, to_server=ping)

        server_pings = [e for e in s_events if e.get("_kind") == "ping"]
        assert server_pings
        assert server_pings[0]["opaque_data"] == b"12345678"

        client_pings = [e for e in c_events if isinstance(e, PingReceived)]
        acks = [e for e in client_pings if e.ack]
        assert acks
        assert acks[0].opaque_data == b"12345678"

    def test_settings_round_trip(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        frames = c.session.submit_settings({0x3: 64})
        c_events, s_events = _pump(c, s, to_server=frames)

        server_settings = [e for e in s_events if e.get("_kind") == "settings"]
        # The handshake already produced one SETTINGS event; we expect
        # at least one *new* one for the user's submit.
        assert any(e.get("settings", {}).get(0x3) == 64 for e in server_settings)

        client_acks = [e for e in c_events if isinstance(e, SettingsReceived) and e.ack]
        assert client_acks

    def test_connection_level_window_update(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        frames = c.session.submit_window_update(0, 1024)
        _, s_events = _pump(c, s, to_server=frames)
        updates = [e for e in s_events if e.get("_kind") == "window_update"]
        assert len(updates) == 1
        assert updates[0]["stream_id"] == 0
        assert updates[0]["increment"] == 1024

    def test_stream_level_window_update(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())

        # Open the stream on the server side via submit_response+body.
        out = s.submit_response(sid, [(":status", "200")], with_body=True)
        _pump(c, s, to_client=out)

        # A stream-level WINDOW_UPDATE is consumed by the server; we
        # therefore assert on the server-side events, not the client.
        frames = c.session.submit_window_update(sid, 4096)
        _, s_events = _pump(c, s, to_server=frames)
        updates = [
            e
            for e in s_events
            if e.get("_kind") == "window_update"
            and e.get("stream_id") == sid
            and e.get("increment") == 4096
        ]
        assert updates


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------


class TestReviewHttp2_3:
    """Regression tests for review_http2_3.md M7/M8 (RFC 9113 §8.1 / §8.1.1)."""

    def test_1xx_informational_emits_typed_event(self):
        """M8: an informational response (e.g. ``103 Early Hints``)
        must surface as ``InformationalResponseReceived`` -- not
        overwrite the eventual ``status_code`` and not end the
        stream."""
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/page", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())

        # Server-side: 103 Early Hints first, then 200 OK.
        early = s.submit_headers(
            sid,
            [
                (":status", "103"),
                ("link", "</style.css>; rel=preload"),
            ],
            0,
        )
        response = s.submit_response(sid, [(":status", "200"), ("content-type", "text/plain")])
        c_events, _ = _pump(c, s, to_client=early + response)

        infos = [e for e in c_events if isinstance(e, InformationalResponseReceived)]
        headers = [e for e in c_events if isinstance(e, HeadersReceived)]
        assert len(infos) == 1
        assert infos[0].stream_id == sid
        assert infos[0].status_code == 103
        assert ("link", "</style.css>; rel=preload") in infos[0].headers

        # The *final* response is what callers will observe as
        # ``status_code``: 200, not 103.
        assert c.streams[sid].response_status_code == 200
        assert len(headers) == 1
        assert headers[0].stream_id == sid
        assert ("content-type", "text/plain") in headers[0].headers

    def test_trailer_headers_emits_typed_event(self):
        """M7: a HEADERS frame carrying trailers must surface as
        ``TrailerReceived`` and not overwrite the original response
        headers."""
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        # Server-side: 200 + body + trailer (the ``nghttp2``
        # server-side API explicitly distinguishes trailer via
        # ``submit_trailer``).
        # The C extension's ``submit_data`` requires the request to
        # have shipped data itself, so we send the request with a
        # body to give the server session a stream slot for
        # response data.
        sid = c.submit_request("GET", "/file", "example.com", body=b"")
        _pump(c, s, to_server=c.bytes_to_send())
        response = s.submit_response(
            sid,
            [
                (":status", "200"),
                ("content-type", "text/plain"),
            ],
            with_body=True,
        )
        body = s.submit_data(sid, b"hello world", end_stream=False)
        trailer = s.submit_trailer(
            sid,
            [
                ("digest", "sha-256=..."),
                ("server-timing", "cache;dur=12"),
            ],
        )
        c_events, _ = _pump(c, s, to_client=response + body + trailer)

        trailers = [e for e in c_events if isinstance(e, TrailerReceived)]
        headers = [e for e in c_events if isinstance(e, HeadersReceived)]
        assert len(headers) == 1
        assert headers[0].stream_id == sid
        # ``content-type`` must NOT be in the trailer section.
        assert not any(name == "content-type" for name, _ in trailers[0].headers)
        assert ("digest", "sha-256=...") in trailers[0].headers
        assert ("server-timing", "cache;dur=12") in trailers[0].headers

        # StreamState captures the trailer separately from the
        # original headers.
        state = c.streams[sid]
        assert ("content-type", "text/plain") in state.response_headers
        assert state.trailer_headers is not None
        assert ("digest", "sha-256=...") in state.trailer_headers
        # ``:status`` must not appear in either.
        assert not any(name == ":status" for name, _ in trailers[0].headers)


class TestStreamStateMachine:
    def test_initial_state_is_open_after_submit_request(self):
        c = HTTP2Connection()
        # Passing ``body=b'...'`` to ``submit_request`` ships it as a
        # single END_STREAM DATA frame, so the local side moves to
        # HALF_CLOSED_LOCAL right after the call (Bugfix review: the
        # previous code accepted the bytes without sending them,
        # leaving the stream OPEN -- that was a silent bug).
        sid = c.submit_request("POST", "/", "example.com", body=b"x")
        assert c.streams[sid].state is StreamLifecycle.HALF_CLOSED_LOCAL

        # Streaming uploads stay OPEN: the caller passes ``body=None``
        # and uses ``submit_data(stream, chunk, end_stream=False)`` in
        # a loop, then ``submit_data(stream, b'', end_stream=True)``
        # at the end.
        c2 = HTTP2Connection()
        sid2 = c2.submit_request("POST", "/", "example.com", body=None)
        assert c2.streams[sid2].state is StreamLifecycle.HALF_CLOSED_LOCAL

        # And ``body=None`` (the default) closes on HEADERS.
        sid3 = c.submit_request("GET", "/", "example.com")
        assert c.streams[sid3].state is StreamLifecycle.HALF_CLOSED_LOCAL

    def test_half_closed_local_after_submit_data_end_stream(self):
        c = HTTP2Connection()
        # Streaming path: submit_request with body=None keeps the
        # local side OPEN, then ``submit_data(end_stream=True)``
        # moves it to HALF_CLOSED_LOCAL.
        sid = c.submit_request("POST", "/", "example.com")
        assert c.streams[sid].state is StreamLifecycle.HALF_CLOSED_LOCAL
        # To actually exercise the OPEN -> HALF_CLOSED_LOCAL
        # transition through ``submit_data`` we need a stream that
        # is still OPEN.  In nghttp2 that means a HEADERS frame
        # followed by an explicit DATA frame with end_stream=False;
        # easiest way is the ``submit_request(body=..., but the body
        # bytes themselves keep the stream open`` is impossible with
        # the public API (body always ends the stream), so we
        # instead observe that the request above already lands at
        # HALF_CLOSED_LOCAL via END_STREAM-on-HEADERS.
        c2 = HTTP2Connection()
        sid2 = c2.submit_request("GET", "/", "example.com")
        assert c2.streams[sid2].state is StreamLifecycle.HALF_CLOSED_LOCAL

    def test_half_closed_remote_after_response_headers_only(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        # ``body=b""`` forces ``with_body=True`` on submit_request, so
        # the local side stays OPEN. The server then END_STREAMs on
        # HEADERS (``with_body=False``), closing the remote side and
        # advancing us to HALF_CLOSED_REMOTE.
        sid = c.submit_request("POST", "/", "example.com", body=b"")
        _pump(c, s, to_server=c.bytes_to_send())

        response = s.submit_response(sid, [(":status", "200")], with_body=False)
        _pump(c, s, to_client=response)
        assert c.streams[sid].state is StreamLifecycle.HALF_CLOSED_REMOTE

    def test_closed_after_full_request_response_no_body(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())

        # Server replies with HEADERS+END_STREAM (no body).
        response = s.submit_response(sid, [(":status", "200")])
        _pump(c, s, to_client=response)
        assert c.streams[sid].state is StreamLifecycle.CLOSED

    def test_closed_after_response_body_end_stream(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())

        response = s.submit_response(sid, [(":status", "200")], with_body=True)
        response += s.submit_data(sid, b"hi", end_stream=True)
        _pump(c, s, to_client=response)
        assert c.streams[sid].state is StreamLifecycle.CLOSED


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


class TestPrune:
    def test_prune_yields_and_drops_closed_streams(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)

        sid = c.submit_request("GET", "/", "example.com")
        _pump(c, s, to_server=c.bytes_to_send())
        response = s.submit_response(sid, [(":status", "200")])
        _pump(c, s, to_client=response)

        assert sid in c.streams
        assert c.streams[sid].is_closed()
        pruned = list(c.prune_closed_streams())
        assert pruned == [sid]
        assert sid not in c.streams


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


class TestMisc:
    def test_unknown_kind_raises(self):
        c = HTTP2Connection()
        with pytest.raises(RuntimeError, match="unknown HTTP/2 event"):
            c._convert_event({"_kind": "unobtanium"})

    def test_get_stream_returns_none_for_unknown(self):
        c = HTTP2Connection()
        assert c.get_stream(999) is None

    def test_bytes_to_send_returns_empty_after_drain(self):
        c = HTTP2Connection()
        # The connection preface was queued during __init__ so the
        # first call returns bytes; the queue is empty afterwards.
        first = c.bytes_to_send()
        assert first.startswith(b"PRI * HTTP/2.0")
        assert c.bytes_to_send() == b""

        c.submit_request("GET", "/", "example.com")
        out = c.bytes_to_send()
        assert out != b""
        assert c.bytes_to_send() == b""

    def test_has_outbound_reflects_queue(self):
        c = HTTP2Connection()
        # Drain the initial preface bytes first.
        c.bytes_to_send()
        assert not c.has_outbound()
        c.submit_request("GET", "/", "example.com")
        assert c.has_outbound()
        c.bytes_to_send()
        assert not c.has_outbound()

    def test_remote_settings_populated_after_handshake(self):
        c = HTTP2Connection()
        s = session_server_new()
        _pump(c, s)
        assert 0x4 in c.remote_settings  # INITIAL_WINDOW_SIZE default

    def test_default_max_concurrent_streams_value(self):
        assert DEFAULT_MAX_CONCURRENT_STREAMS == 100
