import os
import sys
import tempfile
import pytest
import json
from contextlib import contextmanager
from geventhttpclient import HTTPClient
from gevent.ssl import SSLError #@UnresolvedImport
import gevent.pool

import gevent.server
import gevent.pywsgi
from six.moves import xrange

listener = ('127.0.0.1', 54323)

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

@contextmanager
def wsgiserver(handler):
    server = gevent.pywsgi.WSGIServer(('127.0.0.1', 54323), handler)
    server.start()
    try:
        yield
    finally:
        server.stop()

@pytest.mark.online
def test_client_simple():
    client = HTTPClient('httpbin.org')
    assert client.port == 80
    response = client.get('/')
    assert response.status_code == 200
    body = response.read()
    assert len(body)

@pytest.mark.online
def test_client_without_leading_slash():
    client = HTTPClient('httpbin.org')
    with client.get("") as response:
        assert response.status_code == 200
    with client.get("base64/test") as response:
        assert(response.status_code in (200, 301, 302))

test_headers = {'User-Agent': 'Mozilla/5.0 (X11; U; Linux i686; de; rv:1.9.2.17) Gecko/20110422 Ubuntu/10.04 (lucid) Firefox/3.6.17'}
@pytest.mark.online
def test_client_with_default_headers():
    client = HTTPClient.from_url('httpbin.org/', headers=test_headers)

@pytest.mark.online
def test_request_with_headers():
    client = HTTPClient('httpbin.org')
    response = client.get('/', headers=test_headers)
    assert response.status_code == 200

client = HTTPClient('www.heise.de')
raw_req_cmp = client._build_request('GET', '/tp/')

@pytest.mark.online
def test_build_request_relative_uri():
    raw_req = client._build_request('GET', 'tp/')
    assert raw_req == raw_req_cmp

@pytest.mark.online
def test_build_request_absolute_uri():
    raw_req = client._build_request('GET', '/tp/')
    assert raw_req == raw_req_cmp

@pytest.mark.online
def test_build_request_full_url():
    raw_req = client._build_request('GET', 'http://www.heise.de/tp/')
    assert raw_req == raw_req_cmp

@pytest.mark.online
def test_build_request_invalid_host():
    with pytest.raises(ValueError):
        client._build_request('GET', 'http://www.spiegel.de/')

@pytest.mark.online
def test_response_context_manager():
    client = HTTPClient.from_url('http://httpbin.org/')
    r = None
    with client.get('/') as response:
        assert response.status_code == 200
        r = response
    assert r._sock is None # released

@pytest.mark.skipif(
    os.environ.get("TRAVIS") == "true",
    reason="We have issues on travis with the SSL tests"
)
@pytest.mark.online
def test_client_ssl():
    client = HTTPClient('github.com', ssl=True)
    assert client.port == 443
    response = client.get('/')
    assert response.status_code == 200
    body = response.read()
    assert len(body)

@pytest.mark.skipif(
    sys.version_info < (2, 7)
    and os.environ.get("TRAVIS") == "true",
    reason="We have issues on travis with the SSL tests"
)
@pytest.mark.online
def test_ssl_fail_invalid_certificate():
    certs = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "oncert.pem")
    client = HTTPClient('github.com', ssl_options={'ca_certs': certs})
    assert client.port == 443
    with pytest.raises(SSLError) as e_info:
        client.get('/')
    assert e_info.value.reason == 'CERTIFICATE_VERIFY_FAILED'

@pytest.mark.online
def test_multi_queries_greenlet_safe():
    client = HTTPClient('httpbin.org', concurrency=3)
    group = gevent.pool.Group()
    event = gevent.event.Event()

    def run(i):
        event.wait()
        response = client.get('/')
        return response, response.read()

    count = 0

    gevent.spawn_later(0.2, event.set)
    for response, content in group.imap_unordered(run, xrange(5)):
        assert response.status_code == 200
        assert len(content)
        count += 1
    assert count == 5


class StreamTestIterator(object):

    def __init__(self, sep, count):
        lines = [json.dumps({
                 'index': i,
                 'title': 'this is line %d' % i})
                 for i in xrange(0, count)]
        self.buf = (sep.join(lines) + sep).encode()

    def __len__(self):
        return len(self.buf)

    def __iter__(self):
        self.cursor = 0
        return self

    def next(self):
        if self.cursor >= len(self.buf):
            raise StopIteration()

        gevent.sleep(0)
        pos = self.cursor + 10
        data = self.buf[self.cursor:pos]
        self.cursor = pos

        return data

    def __next__(self):
        return self.next()


def readline_iter(sock, addr):
    sock.recv(1024)
    iterator = StreamTestIterator("\n", 100)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n")
    for block in iterator:
        sock.sendall(block)

def test_readline():
    with server(readline_iter):
        client = HTTPClient(*listener, block_size=1)
        response = client.get('/')
        lines = []
        while True:
            line = response.readline(b"\n")
            if not line:
                break
            data = json.loads(line[:-1].decode())
            lines.append(data)
        assert len(lines) == 100
        assert [x['index'] for x in lines] == [x for x in range(0, 100)]

def readline_multibyte_sep(sock, addr):
    sock.recv(1024)
    iterator = StreamTestIterator("\r\n", 100)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n")
    for block in iterator:
        sock.sendall(block)

def test_readline_multibyte_sep():
    with server(readline_multibyte_sep):
        client = HTTPClient(*listener, block_size=1)
        response = client.get('/')
        lines = []
        while True:
            line = response.readline(b"\r\n")
            if not line:
                break
            data = json.loads(line[:-1].decode())
            lines.append(data)
        assert len(lines) == 100
        assert [x['index'] for x in lines] == [x for x in range(0, 100)]

def readline_multibyte_splitsep(sock, addr):
    sock.recv(1024)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n")
    sock.sendall(b'{"a": 1}\r')
    gevent.sleep(0)
    sock.sendall(b'\n{"a": 2}\r\n{"a": 3}\r\n')

def test_readline_multibyte_splitsep():
    with server(readline_multibyte_splitsep):
        client = HTTPClient(*listener, block_size=1)
        response = client.get('/')
        lines = []
        last_index = 0
        while True:
            line = response.readline(b"\r\n")
            if not line:
                break
            data = json.loads(line[:-2].decode())
            assert data['a'] == last_index + 1
            last_index = data['a']
        len(lines) == 3

def internal_server_error(sock, addr):
    sock.recv(1024)
    head = 'HTTP/1.1 500 Internal Server Error\r\n' \
           'Connection: close\r\n' \
           'Content-Type: text/html\r\n' \
           'Content-Length: 135\r\n\r\n'

    body = '<html>\n  <head>\n    <title>Internal Server Error</title>\n  ' \
           '</head>\n  <body>\n    <h1>Internal Server Error</h1>\n    \n  ' \
           '</body>\n</html>\n\n'

    sock.sendall((head + body).encode())
    sock.close()

def test_internal_server_error():
    with server(internal_server_error):
        client = HTTPClient(*listener)
        response = client.get('/')
        assert not response.should_keep_alive()
        assert response.should_close()
        body = response.read()
        assert len(body) == response.content_length

def check_upload(body, body_length):
    def wsgi_handler(env, start_response):
        assert int(env.get('CONTENT_LENGTH')) == body_length
        assert body == env['wsgi.input'].read()
        start_response('200 OK', [])
        return []
    return wsgi_handler

def test_file_post():
    body = tempfile.NamedTemporaryFile("a+b", delete=False)
    name = body.name
    try:
        body.write(b"123456789")
        body.close()
        with wsgiserver(check_upload(b"123456789", 9)):
            client = HTTPClient(*listener)
            with open(name, 'rb') as body:
                client.post('/', body)
    finally:
        os.remove(name)

def test_bytes_post():
    with wsgiserver(check_upload(b"12345", 5)):
        client = HTTPClient(*listener)
        client.post('/', b"12345")

def test_string_post():
    with wsgiserver(check_upload("12345", 5)):
        client = HTTPClient(*listener)
        client.post('/', "12345")

def test_unicode_post():
    byte_string = b'\xc8\xb9\xc8\xbc\xc9\x85'
    unicode_string = byte_string.decode('utf-8')
    with wsgiserver(check_upload(byte_string, len(byte_string))):
        client = HTTPClient(*listener)
        client.post('/', unicode_string)
