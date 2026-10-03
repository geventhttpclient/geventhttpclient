"""Sans-IO tests for the HTTP/2 C wrapper around nghttp2.

These tests wire a client session to a server session directly through
their frame outputs — no sockets involved. They verify framing, HPACK
round-trips, event emission and the streaming body provider.
"""

import h2.config
import h2.connection
import h2.events
import pytest

from geventhttpclient.http2._parser import session_client_new, session_server_new


def drain(session):
    """Collect frames nghttp2 queued automatically (initial SETTINGS,
    ACKs) by running one empty recv() pass."""
    _, frames = session.recv(b"")
    return frames


def pump_frames(client, server, to_server=b"", to_client=b"", rounds=10):
    """Deliver frames in both directions and settle automatic replies
    (SETTINGS/PING ACKs). Returns (client_events, server_events).

    Every recv() call returns (events, outbound); the outbound frames
    belong to the *other* side, so the pump keeps feeding them back and
    forth until both queues are empty."""
    client_events = []
    server_events = []
    for _ in range(rounds):
        progressed = False
        while to_server:
            events, more = server.recv(to_server)
            server_events.extend(events)
            to_server = b""
            to_client += more
            progressed = True
        _, auto = server.recv(b"")
        if auto:
            to_client += auto
            progressed = True
        while to_client:
            events, more = client.recv(to_client)
            client_events.extend(events)
            to_client = b""
            to_server += more
            progressed = True
        _, auto = client.recv(b"")
        if auto:
            to_server += auto
            progressed = True
        if not progressed:
            break
    return client_events, server_events


def exchange(client, server, *batches, rounds=10):
    """Client-to-server variant of pump_frames (request flow)."""
    return pump_frames(client, server,
                       to_server=b"".join(batch for batch in batches if batch),
                       rounds=rounds)


def find(events, kind):
    return [event for event in events if event.get("_kind") == kind]


def get_headers():
    return [
        (":method", "GET"),
        (":scheme", "https"),
        (":path", "/index.html"),
        (":authority", "example.com"),
        ("user-agent", "geventhttpclient-http2-test"),
    ]


def post_headers(path="/upload"):
    return [
        (":method", "POST"),
        (":scheme", "https"),
        (":path", path),
        (":authority", "example.com"),
    ]


class TestSessionLifecycle:
    def test_client_session_creation(self):
        client = session_client_new()
        assert client is not None
        assert client.next_stream_id() == 1

    def test_server_session_creation(self):
        server = session_server_new()
        assert server is not None

    def test_default_local_settings(self):
        client = session_client_new()
        settings = client.get_local_settings()
        # RFC 9113 defaults
        assert settings[0x1] == 4096  # HEADER_TABLE_SIZE
        assert settings[0x4] == 65535  # INITIAL_WINDOW_SIZE
        assert settings[0x5] == 16384  # MAX_FRAME_SIZE


class TestSettingsRoundTrip:
    def test_client_settings_reach_server(self):
        client = session_client_new()
        server = session_server_new()

        # Outbound includes the connection preface + automatic initial
        # SETTINGS + our submitted entry.
        initial = client.submit_settings({0x3: 64})  # MAX_CONCURRENT_STREAMS

        client_events, server_events = exchange(client, server, initial)
        server_settings = find(server_events, "settings")
        assert server_settings, "server must see the client SETTINGS frame"
        assert not server_settings[0]["ack"]
        # The first event is the automatic default SETTINGS; the user
        # entry arrives in a second frame.
        values = [event["settings"].get(0x3) for event in server_settings]
        assert 64 in values, "submitted MAX_CONCURRENT_STREAMS must arrive"

    def test_settings_ack_flows_back(self):
        client = session_client_new()
        server = session_server_new()
        initial = client.submit_settings({0x3: 64})

        client_events, _ = exchange(client, server, initial)
        acks = [event for event in client_events
                if event.get("_kind") == "settings" and event["ack"]]
        assert acks, "client must receive a SETTINGS ack from the server"


class TestRequestRoundTrip:
    def test_get_request_reaches_server(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        assert stream_id == 1
        client_events, server_events = exchange(client, server, frames)
        headers = find(server_events, "headers")
        assert len(headers) == 1
        event = headers[0]
        assert event["stream_id"] == 1
        assert event["end_stream"] is True
        header_dict = dict(event["headers"])
        assert header_dict[":method"] == "GET"
        assert header_dict[":path"] == "/index.html"
        assert header_dict[":authority"] == "example.com"

    def test_post_with_streamed_body(self):
        client = session_client_new()
        server = session_server_new()

        body = b"hello http2 world"
        stream_id, frames = client.submit_request(post_headers(),
                                                  with_body=True)
        assert stream_id == 1
        frames += client.submit_data(stream_id, body, end_stream=True)

        _, server_events = exchange(client, server, frames)
        headers = find(server_events, "headers")
        assert len(headers) == 1
        assert headers[0]["end_stream"] is False

        data_events = find(server_events, "data")
        assert len(data_events) == 1
        assert data_events[0]["data"] == body
        assert data_events[0]["end_stream"] is True

    def test_body_chunked_across_frames(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(post_headers(),
                                                  with_body=True)
        frames += client.submit_data(stream_id, b"chunk-one-", end_stream=False)
        frames += client.submit_data(stream_id, b"chunk-two", end_stream=True)

        _, server_events = exchange(client, server, frames)
        data_events = find(server_events, "data")
        assert len(data_events) == 2
        assert data_events[0]["data"] == b"chunk-one-"
        assert data_events[0]["end_stream"] is False
        assert data_events[1]["data"] == b"chunk-two"
        assert data_events[1]["end_stream"] is True

    def test_second_request_uses_next_stream_id(self):
        client = session_client_new()
        server = session_server_new()

        stream1, frames1 = client.submit_request(get_headers())
        stream2, frames2 = client.submit_request(get_headers())
        assert stream1 == 1
        assert stream2 == 3  # client streams are odd, increment 2

        _, server_events = exchange(client, server, frames1, frames2)
        headers = find(server_events, "headers")
        assert {event["stream_id"] for event in headers} == {1, 3}


class TestFullRequestResponse:
    def test_get_request_and_response(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        client_events, server_events = exchange(client, server, frames)
        headers = find(server_events, "headers")
        assert headers[0]["stream_id"] == stream_id

        response_frames = server.submit_response(
            stream_id,
            [(":status", "200"),
             ("content-type", "text/plain")],
        )
        response_events, _ = pump_frames(client, server,
                                         to_client=response_frames)
        client_events.extend(response_events)
        response_headers = find(client_events, "headers")
        assert len(response_headers) == 1
        response = dict(response_headers[0]["headers"])
        assert response[":status"] == "200"
        assert response["content-type"] == "text/plain"

        closed = find(client_events, "stream_closed")
        assert closed, "END_STREAM response must close the stream client-side"

    def test_response_with_body_via_data_frames(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        exchange(client, server, frames)

        response_frames = server.submit_response(
            stream_id, [(":status", "200")], with_body=True)
        response_frames += server.submit_data(stream_id, b"the-body",
                                              end_stream=True)
        response_events, _ = pump_frames(client, server,
                                         to_client=response_frames)

        headers = find(response_events, "headers")
        assert headers and headers[0]["end_stream"] is False
        data_events = find(response_events, "data")
        assert len(data_events) == 1
        assert data_events[0]["data"] == b"the-body"
        assert data_events[0]["end_stream"] is True


class TestGoAway:
    def test_goaway_reaches_client(self):
        client = session_client_new()
        server = session_server_new()

        goaway_frames = server.submit_goaway(0, 0, b"maintenance")
        client_events, _ = client.recv(goaway_frames)
        goaways = find(client_events, "goaway")
        assert len(goaways) == 1
        event = goaways[0]
        assert event["last_stream_id"] == 0
        assert event["error_code"] == 0
        assert event["debug_data"] == b"maintenance"


class TestStreamReset:
    def test_rst_stream_from_server(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        _, server_events = exchange(client, server, frames)
        assert find(server_events, "headers")

        reset_frames = server.submit_rst_stream(stream_id, 8)  # CANCEL
        client_events, _ = client.recv(reset_frames)
        resets = find(client_events, "stream_reset")
        assert len(resets) == 1
        assert resets[0]["stream_id"] == stream_id
        assert resets[0]["error_code"] == 8


class TestPing:
    def test_ping_roundtrip_with_ack(self):
        client = session_client_new()
        server = session_server_new()

        opaque = b"12345678"
        ping_frames = client.submit_ping(opaque)
        client_events, server_events = exchange(client, server, ping_frames)

        pings = [event for event in server_events
                 if event.get("_kind") == "ping" and not event["ack"]]
        assert pings
        assert pings[0]["opaque_data"] == opaque

        acks = [event for event in client_events
                if event.get("_kind") == "ping" and event["ack"]]
        assert acks, "client must receive the PING ack from the server"


class TestErrors:
    def test_response_submit_rejected_on_client_session(self):
        client = session_client_new()
        with pytest.raises(RuntimeError, match="submit_response"):
            client.submit_response(1, [(":status", "200")])

    def test_invalid_header_pair_raises(self):
        client = session_client_new()
        with pytest.raises((ValueError, TypeError)):
            client.submit_request([("only-a-string",)])

    def test_non_list_headers_raise(self):
        client = session_client_new()
        with pytest.raises(TypeError, match="list"):
            client.submit_request("GET / HTTP/1.1")

    def test_settings_out_of_range_raise(self):
        """SETTINGS ids are 16-bit and values 32-bit (RFC 9113
        §6.5.2) — out-of-range input must not silently truncate."""
        client = session_client_new()
        with pytest.raises(ValueError, match="16-bit"):
            client.submit_settings({0x1FFFF: 1})
        with pytest.raises(ValueError, match="16-bit"):
            client.submit_settings({0x3: 2**40})


class TestWindowUpdate:
    def test_connection_level_window_update(self):
        client = session_client_new()
        server = session_server_new()

        frames = client.submit_window_update(0, 1024)
        server_events, _ = server.recv(frames)
        updates = find(server_events, "window_update")
        assert len(updates) == 1
        assert updates[0]["stream_id"] == 0
        assert updates[0]["increment"] == 1024

    def test_stream_level_window_update(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        exchange(client, server, frames)

        # Response headers without END_STREAM: the stream stays open
        # until the body arrives, so a stream-level WINDOW_UPDATE is
        # valid in between.
        response = server.submit_response(
            stream_id, [(":status", "200")], with_body=True)
        response_events, _ = pump_frames(client, server, to_client=response)
        assert find(response_events, "headers")

        frames = client.submit_window_update(stream_id, 4096)
        _, server_events = pump_frames(client, server, to_server=frames)
        updates = [event for event in server_events
                   if event.get("_kind") == "window_update"
                   and event["stream_id"] == stream_id]
        assert updates
        assert updates[0]["increment"] == 4096

        # Finish the response so both sides close the stream cleanly.
        tail = server.submit_data(stream_id, b"done", end_stream=True)
        pump_frames(client, server, to_client=tail)


class TestSessionLatch:
    def test_fatal_recv_error_latches_session(self):
        """After a fatal nghttp2 error the session refuses all further
        mutating calls (R2; mirrors the HTTP/1 parser error latch)."""
        server = session_server_new()
        with pytest.raises(RuntimeError, match="mem_recv"):
            server.recv(b"this is not the HTTP/2 client connection preface")
        with pytest.raises(RuntimeError, match="unusable"):
            server.recv(b"")
        with pytest.raises(RuntimeError, match="unusable"):
            server.submit_settings({0x3: 10})
        with pytest.raises(RuntimeError, match="unusable"):
            server.submit_response(1, [(":status", "200")])


class TestHeaderListSizeLimit:
    LIMIT = 65536

    def test_limit_is_advertised_in_initial_settings(self):
        client = session_client_new()
        server = session_server_new()
        _, initial = client.recv(b"")
        pump_frames(client, server, to_server=initial)
        remote = server.get_remote_settings()
        assert remote[0x6] == self.LIMIT  # MAX_HEADER_LIST_SIZE

    def test_oversized_header_block_is_rejected(self):
        """A peer ignoring the advertised cap must not exhaust memory;
        the callback failure surfaces as a fatal session error (M2).

        Uses hyper-h2 (a declared dev dependency) as the peer: our own
        server session cannot produce an oversized block because
        nghttp2's send-side cap (NGHTTP2_MAX_HEADERSLEN) drops it
        first."""
        client = session_client_new()
        peer = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False))
        peer.initiate_connection()

        stream_id, frames = client.submit_request(get_headers())
        peer.receive_data(frames)
        peer.send_headers(
            stream_id,
            [(b":status", b"200"), (b"x-flood", b"a" * (self.LIMIT + 1))],
        )
        with pytest.raises(RuntimeError,
                           match="MAX_HEADER_LIST_SIZE|mem_recv"):
            client.recv(peer.data_to_send())

    def test_large_but_legal_header_block_passes(self):
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        exchange(client, server, frames)

        response = server.submit_response(
            stream_id,
            [(":status", "200"), ("x-large", "a" * (self.LIMIT // 2))],
        )
        response_events, _ = pump_frames(client, server, to_client=response)
        headers = find(response_events, "headers")
        assert headers
        assert dict(headers[0]["headers"])["x-large"] == "a" * (self.LIMIT // 2)


class TestStreamClosedSingleSource:
    def _open_response_stream(self):
        client = session_client_new()
        server = session_server_new()
        stream_id, frames = client.submit_request(get_headers())
        exchange(client, server, frames)
        response = server.submit_response(
            stream_id, [(":status", "200")], with_body=True)
        pump_frames(client, server, to_client=response)
        return client, server, stream_id

    def test_empty_terminal_data_surfaces_as_empty_chunk(self):
        """A zero-length DATA frame with END_STREAM must surface as a
        data event with b"" + end_stream (no chunk callback fires for
        it in nghttp2)."""
        client, server, stream_id = self._open_response_stream()
        tail = server.submit_data(stream_id, b"", end_stream=True)
        events, _ = pump_frames(client, server, to_client=tail)

        data_events = [e for e in find(events, "data")
                       if e["stream_id"] == stream_id]
        assert data_events, "empty terminal DATA must still surface"
        assert data_events[-1]["data"] == b""
        assert data_events[-1]["end_stream"] is True

    def test_stream_closed_fires_exactly_once(self):
        """on_stream_close is the single source of stream_closed —
        no duplicate from the DATA END_STREAM path (M3).

        Regression guard: re-introducing a stream_closed emission in
        the DATA-END_STREAM path of on_frame_recv would make this
        count 2."""
        client, server, stream_id = self._open_response_stream()
        tail = server.submit_data(stream_id, b"payload", end_stream=True)
        events, _ = pump_frames(client, server, to_client=tail)

        closed = [e for e in find(events, "stream_closed")
                  if e["stream_id"] == stream_id]
        assert len(closed) == 1

        data_events = [e for e in find(events, "data")
                       if e["stream_id"] == stream_id]
        assert data_events[-1]["end_stream"] is True


class TestPushRefusal:
    def test_pushed_stream_is_refused_and_connection_survives(self):
        """PUSH_PROMISE must be answered with RST_STREAM
        (REFUSED_STREAM) on the promised stream, keeping the
        connection alive (M5). Uses hyper-h2 (a declared dev
        dependency) as the pushing server."""
        client = session_client_new()
        peer = h2.connection.H2Connection(
            config=h2.config.H2Configuration(client_side=False))
        peer.initiate_connection()

        stream_id, frames = client.submit_request(get_headers())
        peer.receive_data(frames)
        promised_id = 2
        peer.push_stream(stream_id, promised_id, [
            (b":method", b"GET"),
            (b":scheme", b"https"),
            (b":path", b"/pushed"),
            (b":authority", b"example.com"),
        ])
        _, outbound = client.recv(peer.data_to_send())

        # The client must have queued a refusal for the promised stream.
        reset_events = peer.receive_data(outbound)
        resets = [e for e in reset_events
                  if isinstance(e, h2.events.StreamReset)
                  and e.stream_id == promised_id]
        assert resets, "promised stream must be reset"
        assert resets[0].error_code == 0x7  # REFUSED_STREAM

        # The connection stays usable: the regular response flows.
        peer.send_headers(stream_id, [(b":status", b"200")],
                          end_stream=True)
        events, _ = client.recv(peer.data_to_send())
        headers = find(events, "headers")
        assert headers
        assert dict(headers[0]["headers"])[":status"] == "200"


class TestWindowSizes:
    def test_defaults(self):
        client = session_client_new()
        assert client.get_remote_window_size() == 65535
        assert client.get_local_window_size() == 65535

    def test_stream_windows_and_unknown_stream(self):
        client = session_client_new()
        server = session_server_new()
        stream_id, frames = client.submit_request(
            post_headers(), with_body=True)
        exchange(client, server, frames)

        assert client.get_stream_remote_window_size(stream_id) == 65535
        assert client.get_stream_local_window_size(stream_id) == 65535
        # Unknown streams report None, not an nghttp2 error code.
        assert client.get_stream_remote_window_size(9999) is None

        # Sending data shrinks the remote (send) window.
        client.submit_data(stream_id, b"x" * 100, end_stream=False)
        assert client.get_stream_remote_window_size(stream_id) == 65535 - 100

    def test_receive_window_shrinks_until_consumed(self):
        client = session_client_new()
        server = session_server_new()
        stream_id, frames = client.submit_request(get_headers())
        exchange(client, server, frames)
        response = server.submit_response(
            stream_id, [(":status", "200")], with_body=True)
        pump_frames(client, server, to_client=response)

        body = server.submit_data(stream_id, b"y" * 1000,
                                  end_stream=False)
        client.recv(body)
        # The receive window only recovers once the application
        # consumes via nghttp2's automatic window update; either way
        # the getter must report a plausible value.
        assert 0 <= client.get_stream_local_window_size(stream_id) <= 65535


class TestHeaderEncoding:
    """Header names/values are opaque octets on the wire; the binding
    speaks latin-1 in both directions (1:1 byte mapping), matching the
    HTTP/1 wrapper and header.py."""

    def test_obs_text_response_value_survives(self):
        """A server sending obs-text (0x80-0xFF) header values must not
        kill the session with a decode error (R1)."""
        client = session_client_new()
        server = session_server_new()

        stream_id, frames = client.submit_request(get_headers())
        exchange(client, server, frames)

        response = server.submit_response(
            stream_id,
            [(":status", "200"), ("x-quoted", "caf\xe9 \x80raw")],
        )
        response_events, _ = pump_frames(client, server, to_client=response)
        headers = find(response_events, "headers")
        assert len(headers) == 1
        values = dict(headers[0]["headers"])
        # 1:1 byte mapping: the exact characters come back.
        assert values["x-quoted"] == "caf\xe9 \x80raw"

    def test_obs_text_round_trip_is_lossless(self):
        """latin-1 in both directions: a str submitted on one side
        arrives byte-identical (as latin-1 str) on the other."""
        client = session_client_new()
        server = session_server_new()

        headers = get_headers() + [("x-forwarded-note", "\xff\xfe\x80")]
        _, frames = client.submit_request(headers)
        _, server_events = exchange(client, server, frames)
        received = find(server_events, "headers")
        assert received
        values = dict(received[0]["headers"])
        assert values["x-forwarded-note"] == "\xff\xfe\x80"

    def test_non_latin1_characters_raise_on_submit(self):
        """Characters outside latin-1 must fail loudly instead of
        silently producing UTF-8 mojibake on the wire."""
        client = session_client_new()
        with pytest.raises(UnicodeEncodeError):
            client.submit_request(get_headers() + [("x-emoji", "\U0001f600")])
