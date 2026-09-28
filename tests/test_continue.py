from geventhttpclient import HTTPClient
from tests.common import LISTENER, server

BODY = b"0123456789" * 100


def read_headers(sock):
    """Read from the socket until the end of the request headers."""
    data = b""
    while b"\r\n\r\n" not in data:
        block = sock.recv(4096)
        assert block, "connection closed before request headers were complete"
        data += block
    return data


def continue_then_response(final_response):
    """Handler asserting the Expect header, answering 100 Continue, then
    reading the body and answering with `final_response`."""

    def handler(sock, addr):
        request = read_headers(sock)
        assert b"expect: 100-continue" in request.lower()
        assert b"content-length: " + str(len(BODY)).encode() in request.lower()
        assert not request.endswith(b"\r\n\r\n" + BODY), "body must not be sent before 100 Continue"
        sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        body = b""
        while len(body) < len(BODY):
            block = sock.recv(4096)
            assert block, "connection closed before the body was complete"
            body += block
        assert body == BODY
        sock.sendall(final_response)

    return handler


def test_100_continue_then_200():
    with server(
        continue_then_response(
            b"HTTP/1.1 200 Ok\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
    ):
        client = HTTPClient(*LISTENER)
        response = client.post("/", body=BODY, headers={"Expect": "100-continue"})
        assert response.status_code == 200
        assert response.read() == b"ok"


def test_100_continue_rejected_without_body():
    """The server rejects with 401 right after the headers; the body must
    never be sent and the 401 response returned instead."""

    def handler(sock, addr):
        request = read_headers(sock)
        assert b"expect: 100-continue" in request.lower()
        sock.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        sock.close()

    with server(handler):
        client = HTTPClient(*LISTENER)
        response = client.post("/", body=BODY, headers={"Expect": "100-continue"})
        assert response.status_code == 401


def test_100_continue_interim_and_final_in_one_segment():
    """The server sends 100 Continue and the final response in a single
    segment, before the body has even been sent."""

    def handler(sock, addr):
        request = read_headers(sock)
        assert b"expect: 100-continue" in request.lower()
        sock.sendall(
            b"HTTP/1.1 100 Continue\r\n\r\n"
            b"HTTP/1.1 200 Ok\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"
        )
        # drain the body, the client sends it after the interim response
        body = b""
        while len(body) < len(BODY):
            block = sock.recv(4096)
            assert block
            body += block
        assert body == BODY

    with server(handler):
        client = HTTPClient(*LISTENER)
        response = client.post("/", body=BODY, headers={"Expect": "100-continue"})
        assert response.status_code == 200
        assert response.read() == b"ok"


def test_100_continue_with_chunked_body():
    seen = {}

    def checking_handler(sock, addr):
        request = read_headers(sock)
        seen["chunked"] = b"transfer-encoding: chunked" in request.lower()
        seen["content-length"] = b"content-length" in request.lower()
        sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        body = b""
        while not body.endswith(b"0\r\n\r\n"):
            block = sock.recv(4096)
            assert block, "connection closed before the final chunk"
            body += block
        seen["body"] = body
        sock.sendall(b"HTTP/1.1 200 Ok\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    with server(checking_handler):
        client = HTTPClient(*LISTENER)
        response = client.post(
            "/",
            body=BODY,
            headers={"Expect": "100-continue", "Transfer-Encoding": "chunked"},
        )
        assert response.status_code == 200
        assert response.read() == b"ok"

    assert seen["chunked"] is True
    assert seen["content-length"] is False
    assert BODY in seen["body"]


def test_100_continue_without_body():
    """A bodyless GET with Expect still completes against a 100/200 sequence."""

    def handler(sock, addr):
        request = read_headers(sock)
        assert b"expect: 100-continue" in request.lower()
        sock.sendall(b"HTTP/1.1 100 Continue\r\n\r\n")
        sock.sendall(b"HTTP/1.1 204 No Content\r\nConnection: close\r\n\r\n")

    with server(handler):
        client = HTTPClient(*LISTENER)
        response = client.get("/", headers={"Expect": "100-continue"})
        assert response.status_code == 204
