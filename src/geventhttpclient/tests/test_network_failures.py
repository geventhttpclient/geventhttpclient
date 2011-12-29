import pytest
from httplib import HTTPException
from geventhttpclient import HTTPClient
import gevent.server
import gevent.timeout
import gevent.socket
from contextlib import contextmanager

listener = ('127.0.0.1', 5432)

@contextmanager
def server(handler):
    server = gevent.server.StreamServer(
        listener,
        handle=handler)
    server.start()
    try:
        yield
    finally:
        server.stop()

def wrong_response_status_line(sock, addr):
    sock.recv(4096)
    sock.sendall('HTTP/1.1 apfais df0 asdf\r\n\r\n')

def test_exception():
    with server(wrong_response_status_line):
        connection = HTTPClient(*listener)
        with pytest.raises(HTTPException):
            connection.get('/')

def close(sock, addr):
    sock.close()

def test_close():
    with server(close):
        client = HTTPClient(*listener)
        with pytest.raises(HTTPException):
            client.get('/')

def close_after_recv(sock, addr):
    sock.recv(4096)
    sock.close()

def test_close_after_recv():
    with server(close_after_recv):
        client = HTTPClient(*listener)
        with pytest.raises(HTTPException):
            client.get('/')

def timeout_recv(sock, addr):
    sock.recv(4096)
    gevent.sleep(1)

def test_timeout_recv():
    with server(timeout_recv):
        connection = HTTPClient(*listener, network_timeout=0.1)
        with pytest.raises(gevent.socket.timeout):
            connection.request("GET", '/')

def timeout_send(sock, addr):
    gevent.sleep(1)

def test_timeout_send():
    with server(timeout_send):
        connection = HTTPClient(*listener, network_timeout=0.1)
        with pytest.raises(gevent.socket.timeout):
            connection.request("GET", '/')

def close_during_content(sock, addr):
    sock.recv(4096)
    sock.sendall("""HTTP/1.1 200 Ok\r\nContent-Length: 100\r\n\r\n""")
    sock.close()

def test_close_during_content():
    with server(close_during_content):
        client = HTTPClient(*listener, block_size=1)
        response = client.get('/')
        with pytest.raises(HTTPException):
            response.read()

def content_too_small(sock, addr):
    sock.recv(4096)
    sock.sendall("""HTTP/1.1 200 Ok\r\nContent-Length: 100\r\n\r\ncontent""")
    gevent.sleep(10)

def test_content_too_small():
    with server(content_too_small):
        client = HTTPClient(*listener, network_timeout=0.2)
        with pytest.raises(gevent.socket.timeout):
            response = client.get('/')
            response.read()

