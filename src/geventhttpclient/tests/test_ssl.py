from contextlib import contextmanager
import pytest
import gevent.server
import gevent.socket
import gevent.ssl
import os.path
from geventhttpclient import HTTPClient

BASEDIR = os.path.dirname(__file__)
KEY = os.path.join(BASEDIR, 'server.key')
CERT = os.path.join(BASEDIR, 'server.crt')


listener = ('127.0.0.1', 5432)

@contextmanager
def server(handler, backlog=1):
    server = gevent.server.StreamServer(
        listener,
        backlog=backlog,
        handle=handler,
        keyfile=KEY,
        certfile=CERT)
    server.start()
    try:
        yield
    finally:
        server.stop()

@contextmanager
def timeout_connect_server():
    sock = gevent.socket.socket(gevent.socket.AF_INET, #@UndefinedVariable
        gevent.socket.SOCK_STREAM, 0) #@UndefinedVariable
    sock = gevent.ssl.wrap_socket(sock, keyfile=KEY, certfile=CERT)
    sock.setsockopt(gevent.socket.SOL_SOCKET, gevent.socket.SO_REUSEADDR, 1) #@UndefinedVariable
    print 'bind'
    sock.bind(listener)
    print 'listen'
    sock.listen(1)

    def run(sock):
        conns = []
        while True:
            conn, addr = sock.accept()
            conns.append(conns)
            conn.recv(1024)
            gevent.sleep(10)

    job = gevent.spawn(run, sock)
    try:
        yield
    finally:
        job.kill()

def simple_ssl_response(sock, addr):
    sock.recv(1024)
    sock.sendall('HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n')
    sock.close()

def test_simple_ssl():
    with server(simple_ssl_response):
        http = HTTPClient(*listener, ssl=True, ssl_options={'ca_certs': CERT})
        response = http.get('/')
        assert response.status_code == 200
        response.read()

def timeout_on_connect(sock, addr):
    sock.recv(1024)
    sock.sendall('HTTP/1.1 200 Ok\r\nContent-Length: 0\r\n\r\n')

def test_timeout_on_connect():
    with timeout_connect_server():
        http = HTTPClient(*listener, ssl=True, ssl_options={'ca_certs': CERT})

        def run(http, wait_time=100):
            response = http.get('/')
            gevent.sleep(wait_time)
            response.read()

        gevent.spawn(run, http)
        gevent.sleep(0)

        e = None
        try:
            http2 = HTTPClient(*listener,
                ssl=True,
                connection_timeout=0.1,
                ssl_options={'ca_certs': CERT})
            http2.get('/')
        except gevent.ssl.SSLError as error: #@UndefinedVariable
            e = error
        except gevent.socket.timeout as error: #@UndefinedVariable
            e = error
        except:
            raise

        assert e is not None, 'should have raised'
        if isinstance(e, gevent.ssl.SSLError): #@UndefinedVariable
            assert str(e).endswith("handshake operation timed out")

def network_timeout(sock, addr):
    sock.recv(1024)
    gevent.sleep(10)
    sock.sendall('HTTP/1.1 200 Ok\r\nContent-Length: 0\r\n\r\n')

def test_network_timeout():
    with server(network_timeout):
        http = HTTPClient(*listener, ssl=True,
            network_timeout=0.1, ssl_options={'ca_certs': CERT})
        with pytest.raises(gevent.ssl.SSLError): #@UndefinedVariable
            response = http.get('/')
            assert response.status_code == 0, 'should have timed out.'


