import gevent.ssl

from tests.common import server
from tests.test_proxy import EXPECTED_AUTH, TARGET_HOST, proxy_client, read_request
from tests.test_ssl import CERT, KEY

TLS_SERVER_OPTIONS = {
    "server_side": True,
    "keyfile": KEY,
    "certfile": CERT,
    "ssl_version": gevent.ssl.PROTOCOL_TLS_SERVER,
}


def tls_proxy_handler(expect_auth=True):
    """Answer the CONNECT request, then serve HTTP inside the TLS tunnel."""

    def handler(sock, addr):
        request = read_request(sock)
        header = request.split(b"\r\n\r\n", 1)[0].lower()
        assert header.split(b"\r\n", 1)[0] == (
            b"connect " + TARGET_HOST.encode() + b":443 http/1.1"
        )
        auth_lines = [
            line for line in header.split(b"\r\n") if line.startswith(b"proxy-authorization:")
        ]
        if expect_auth:
            assert auth_lines == [b"proxy-authorization: " + EXPECTED_AUTH.lower()]
        else:
            assert auth_lines == []
        sock.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        tls_sock = gevent.ssl.wrap_socket(sock, **TLS_SERVER_OPTIONS)
        try:
            request = b""
            while b"\r\n\r\n" not in request:
                block = tls_sock.recv(4096)
                assert block, "tunnel closed before request was complete"
                request += block
            assert request.split(b"\r\n", 1)[0] == b"GET /path HTTP/1.1"
            tls_sock.sendall(
                b"HTTP/1.1 200 Ok\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello"
            )
        finally:
            tls_sock.close()

    return handler


def test_https_via_proxy_with_auth():
    with server(tls_proxy_handler(expect_auth=True)):
        client = proxy_client(
            port=443,
            ssl=True,
            insecure=True,
            ssl_options={"ca_certs": CERT},
            proxy_user="user",
            proxy_password="pass",
        )
        response = client.get("/path")
        assert response.status_code == 200
        assert response.read() == b"hello"


def test_https_via_proxy_without_auth():
    with server(tls_proxy_handler(expect_auth=False)):
        client = proxy_client(
            port=443,
            ssl=True,
            insecure=True,
            ssl_options={"ca_certs": CERT},
        )
        response = client.get("/path")
        assert response.status_code == 200
        assert response.read() == b"hello"
