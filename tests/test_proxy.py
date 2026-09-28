import base64

import pytest

from geventhttpclient import HTTPClient
from tests.common import LISTENER, server
from tests.test_ssl import CERT

TARGET_HOST = "target.example.com"
EXPECTED_AUTH = b"Basic " + base64.b64encode(b"user:pass")


def plain_proxy_handler(expect_auth=False):
    """Assert the request arrives with an absolute URI and no CONNECT."""
    expected_auth = b"Basic " + base64.b64encode(b"user:pass")

    def handler(sock, addr):
        data = b""
        while b"\r\n\r\n" not in data:
            block = sock.recv(4096)
            assert block, "connection closed before request was complete"
            data += block
        header = data.split(b"\r\n\r\n", 1)[0]
        first_line = header.split(b"\r\n", 1)[0]
        assert first_line.startswith(b"GET http://" + TARGET_HOST.encode())
        assert first_line.endswith(b"/path HTTP/1.1")
        assert b"CONNECT" not in data
        auth_lines = [
            line
            for line in header.lower().split(b"\r\n")
            if line.startswith(b"proxy-authorization:")
        ]
        if expect_auth:
            assert auth_lines == [b"proxy-authorization: " + expected_auth.lower()]
        else:
            assert auth_lines == []
        sock.sendall(b"HTTP/1.1 200 Ok\r\nContent-Length: 8\r\nConnection: close\r\n\r\nproxied!")

    return handler


def read_request(sock):
    data = b""
    while b"\r\n\r\n" not in data:
        block = sock.recv(4096)
        assert block, "connection closed before request was complete"
        data += block
    return data


def reject_proxy_handler():
    """Reject any CONNECT request with a 407 response."""

    def handler(sock, addr):
        read_request(sock)
        sock.sendall(
            b"HTTP/1.1 407 Proxy Authentication Required\r\n"
            b'Proxy-Authenticate: Basic realm="proxy"\r\n\r\n'
        )
        sock.close()

    return handler


def proxy_client(**kw):
    defaults = {
        "proxy_host": LISTENER[0],
        "proxy_port": LISTENER[1],
    }
    defaults.update(kw)
    return HTTPClient(TARGET_HOST, **defaults)


def test_plain_http_via_proxy():
    with server(plain_proxy_handler()):
        client = proxy_client()
        response = client.get("/path")
        assert response.status_code == 200
        assert response.read() == b"proxied!"


def test_plain_http_via_proxy_with_auth():
    with server(plain_proxy_handler(expect_auth=True)):
        client = proxy_client(proxy_user="user", proxy_password="pass")
        response = client.get("/path")
        assert response.status_code == 200
        assert response.read() == b"proxied!"


def test_proxy_auth_rejected():
    with server(reject_proxy_handler()):
        client = proxy_client(
            port=443,
            ssl=True,
            insecure=True,
            ssl_options={"ca_certs": CERT},
            proxy_user="user",
            proxy_password="wrong",
        )
        with pytest.raises(RuntimeError, match="407"):
            client.get("/path")
