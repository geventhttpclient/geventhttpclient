import six
import pytest
if six.PY2:
    from httplib import HTTPException
else:
    from http.client import HTTPException
from geventhttpclient import HTTPClient
import gevent.server
import gevent.socket
from contextlib import contextmanager

CRLF = "\r\n"

listener = ('127.0.0.1', 54326)

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
    sock.sendall(b'HTTP/1.1 apfais df0 asdf\r\n\r\n')

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
    sock.sendall(b"""HTTP/1.1 200 Ok\r\nContent-Length: 100\r\n\r\n""")
    sock.close()

def test_close_during_content():
    with server(close_during_content):
        client = HTTPClient(*listener, block_size=1)
        response = client.get('/')
        with pytest.raises(HTTPException):
            response.read()

def content_too_small(sock, addr):
    sock.recv(4096)
    sock.sendall(b"""HTTP/1.1 200 Ok\r\nContent-Length: 100\r\n\r\ncontent""")
    gevent.sleep(10)

def test_content_too_small():
    with server(content_too_small):
        client = HTTPClient(*listener, network_timeout=0.2)
        with pytest.raises(gevent.socket.timeout):
            response = client.get('/')
            response.read()

def close_during_chuncked_readline(sock, addr):
    sock.recv(4096)
    sock.sendall(b'HTTP/1.1 200 Ok\r\nTransfer-Encoding: chunked\r\n\r\n')

    chunks = ['This is the data in the first chunk\r\n',
        'and this is the second one\r\n',
        'con\r\n']

    for chunk in chunks:
        gevent.sleep(0.1)
        sock.sendall((hex(len(chunk))[2:] + CRLF + chunk + CRLF).encode())
    sock.close()

def test_close_during_chuncked_readline():
    with server(close_during_chuncked_readline):
        client = HTTPClient(*listener)
        response = client.get('/')
        assert response['transfer-encoding'] == 'chunked'
        chunks = []
        with pytest.raises(HTTPException):
            data = 'enter_loop'
            while data:
                data = response.readline()
                chunks.append(data)
        assert len(chunks) == 3

def timeout_during_chuncked_readline(sock, addr):
    sock.recv(4096)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nTransfer-Encoding: chunked\r\n\r\n")

    chunks = ['This is the data in the first chunk\r\n',
        'and this is the second one\r\n',
        'con\r\n']

    for chunk in chunks:
        sock.sendall((hex(len(chunk))[2:] + CRLF + chunk + CRLF).encode())
    gevent.sleep(2)
    sock.close()

def test_timeout_during_chuncked_readline():
    with server(timeout_during_chuncked_readline):
        client = HTTPClient(*listener, network_timeout=0.1)
        response = client.get('/')
        assert response['transfer-encoding'] == 'chunked'
        chunks = []
        with pytest.raises(gevent.socket.timeout):
            data = 'enter_loop'
            while data:
                data = response.readline()
                chunks.append(data)
        assert len(chunks) == 3

