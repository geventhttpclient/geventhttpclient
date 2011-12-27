import pytest
from httplib import HTTPException
from geventhttpclient.httplib import HTTPConnection
import gevent.server

listener = ('127.0.0.1', 5432)

def wrong_response_status_line(sock, addr):
    print addr
    request = sock.recv(4096)
    print request
    sock.sendall('HTTP/1.1 apfais df0 asdf\r\n\r\n')

def test_httplib_exception():
    server = gevent.server.StreamServer(
        listener,
        handle=wrong_response_status_line)
    server.start()
    try:
        connection = HTTPConnection(*listener)
        connection.request("GET", '/')
        with pytest.raises(HTTPException):
            response = connection.getresponse()
    finally:
        server.stop()


