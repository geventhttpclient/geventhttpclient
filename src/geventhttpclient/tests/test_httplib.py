import pytest
from httplib import HTTPException
from geventhttpclient.httplib import HTTPConnection
import gevent.server
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

def test_httplib_exception():
    with server(wrong_response_status_line):
        connection = HTTPConnection(*listener)
        connection.request("GET", '/')
        with pytest.raises(HTTPException): #@UndefinedVariable
            connection.getresponse()

def success_response(sock, addr):
    sock.recv(4096)
    sock.sendall("HTTP/1.1 200 Ok\r\n"
               "Content-Type: text/plain\r\n"
               "Content-Length: 12\r\n\r\n"
               "Hello World!")

def test_success_response():
    with server(success_response):
        connection = HTTPConnection(*listener)
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.should_keep_alive()
        assert response.message_complete
        assert not response.should_close()
        assert response.read() == 'Hello World!'
        assert response.content_length == 12

