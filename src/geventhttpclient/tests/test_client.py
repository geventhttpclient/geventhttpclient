import json

import gevent.pool
import gevent.queue
import gevent.server
import pytest

from geventhttpclient import __version__
from geventhttpclient.client import METHOD_GET, HTTPClient
from geventhttpclient.tests.conftest import HTTPBIN_HOST, LISTENER, server, wsgiserver


def httpbin_client(
    host=HTTPBIN_HOST,
    port=None,
    headers=None,
    block_size=HTTPClient.BLOCK_SIZE,
    connection_timeout=30.0,
    network_timeout=30.0,
    disable_ipv6=True,
    concurrency=1,
    ssl=False,
    ssl_options=None,
    ssl_context_factory=None,
    insecure=False,
    proxy_host=None,
    proxy_port=None,
    version=HTTPClient.HTTP_11,
):
    """Client factory for httpbin with higher timeout values"""

    return HTTPClient(
        host,
        port=port,
        headers=headers,
        block_size=block_size,
        connection_timeout=connection_timeout,
        network_timeout=network_timeout,
        disable_ipv6=disable_ipv6,
        concurrency=concurrency,
        ssl=ssl,
        ssl_options=ssl_options,
        ssl_context_factory=ssl_context_factory,
        insecure=insecure,
        proxy_host=proxy_host,
        proxy_port=proxy_port,
        version=version,
    )


@pytest.fixture
def httpbin():
    return httpbin_client()


@pytest.mark.parametrize("request_uri", ["/tp/", "tp/", f"http://{HTTPBIN_HOST}/tp/"])
def test_build_request(httpbin, request_uri):
    request_ref = f"GET /tp/ HTTP/1.1\r\nUser-Agent: python/gevent-http-client-{__version__}\r\nHost: {HTTPBIN_HOST}\r\n\r\n"
    assert httpbin._build_request(METHOD_GET, request_uri) == request_ref


def test_build_request_invalid_host(httpbin):
    with pytest.raises(ValueError):
        httpbin._build_request(METHOD_GET, "http://someunrelatedhost.com/")


@pytest.mark.parametrize("port", [None, 1234])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]"])
def test_build_request_host(host, port):
    client = HTTPClient(host, port)
    host_ref = (
        f"host: {f'[{host}]' if host.startswith(':') else host}{f':{port}' if port else ''}\r\n"
    )
    assert host_ref in client._build_request(METHOD_GET, "").lower()


test_url_client_args = [
    ("http://python.org", ("python.org", 80)),
    ("http://python.org:333", ("python.org", 333)),
]


@pytest.mark.parametrize(["url", "client_args"], test_url_client_args)
def test_from_url(url, client_args):
    from_url = HTTPClient.from_url(url)
    from_args = HTTPClient(*client_args)
    assert from_args.host == from_url.host
    assert from_args.port == from_url.port


class StreamTestIterator:
    def __init__(self, sep, count):
        lines = [json.dumps({"index": i, "title": f"this is line {i}"}) for i in range(0, count)]
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
        data = self.buf[self.cursor : pos]
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
        client = HTTPClient(*LISTENER, block_size=1)
        response = client.get("/")
        lines = []
        while True:
            line = response.readline(b"\n")
            if not line:
                break
            data = json.loads(line[:-1].decode())
            lines.append(data)
        assert len(lines) == 100
        assert [x["index"] for x in lines] == [x for x in range(0, 100)]


def readline_multibyte_sep(sock, addr):
    sock.recv(1024)
    iterator = StreamTestIterator("\r\n", 100)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n")
    for block in iterator:
        sock.sendall(block)


def test_readline_multibyte_sep():
    with server(readline_multibyte_sep):
        client = HTTPClient(*LISTENER, block_size=1)
        response = client.get("/")
        lines = []
        while True:
            line = response.readline(b"\r\n")
            if not line:
                break
            data = json.loads(line[:-1].decode())
            lines.append(data)
        assert len(lines) == 100
        assert [x["index"] for x in lines] == [x for x in range(0, 100)]


def readline_multibyte_splitsep(sock, addr):
    sock.recv(1024)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n")
    sock.sendall(b'{"a": 1}\r')
    gevent.sleep(0)
    sock.sendall(b'\n{"a": 2}\r\n{"a": 3}\r\n')


def test_readline_multibyte_splitsep():
    with server(readline_multibyte_splitsep):
        client = HTTPClient(*LISTENER, block_size=1)
        response = client.get("/")
        lines = []
        last_index = 0
        while True:
            line = response.readline(b"\r\n")
            if not line:
                break
            data = json.loads(line[:-2].decode())
            assert data["a"] == last_index + 1
            last_index = data["a"]
        len(lines) == 3


def internal_server_error(sock, addr):
    sock.recv(1024)
    head = (
        "HTTP/1.1 500 Internal Server Error\r\n"
        "Connection: close\r\n"
        "Content-Type: text/html\r\n"
        "Content-Length: 135\r\n\r\n"
    )

    body = (
        "<html>\n  <head>\n    <title>Internal Server Error</title>\n  "
        "</head>\n  <body>\n    <h1>Internal Server Error</h1>\n    \n  "
        "</body>\n</html>\n\n"
    )

    sock.sendall((head + body).encode())
    sock.close()


def test_internal_server_error():
    with server(internal_server_error):
        client = HTTPClient(*LISTENER)
        response = client.get("/")
        assert not response.should_keep_alive()
        assert response.should_close()
        body = response.read()
        assert len(body) == response.content_length


def check_upload(body, body_length):
    def wsgi_handler(env, start_response):
        assert int(env.get("CONTENT_LENGTH")) == body_length
        assert body == env["wsgi.input"].read()
        start_response("200 OK", [])
        return []

    return wsgi_handler


def test_file_post(tmp_path):
    fpath = tmp_path / "tmp_body.txt"
    with open(fpath, "wb") as body:
        body.write(b"123456789")
    with wsgiserver(check_upload(b"123456789", 9)):
        client = HTTPClient(*LISTENER)
        with open(fpath, "rb") as body:
            client.post("/", body)


def test_bytes_post():
    with wsgiserver(check_upload(b"12345", 5)):
        client = HTTPClient(*LISTENER)
        client.post("/", b"12345")


def test_string_post():
    with wsgiserver(check_upload(b"12345", 5)):
        client = HTTPClient(*LISTENER)
        client.post("/", "12345")


def test_unicode_post():
    byte_string = b"\xc8\xb9\xc8\xbc\xc9\x85"
    unicode_string = byte_string.decode("utf-8")
    with wsgiserver(check_upload(byte_string, len(byte_string))):
        client = HTTPClient(*LISTENER)
        client.post("/", unicode_string)


# The tests below require online access. We should try to replace them at least
# partly with local testing solutions and have the online tests as an extra on top.


@pytest.mark.network
def test_client_simple(httpbin):
    assert httpbin.port == 80
    response = httpbin.get("/")
    assert response.status_code == 200
    body = response.read()
    assert len(body)


@pytest.mark.network
def test_client_without_leading_slash(httpbin):
    with httpbin.get("") as response:
        assert response.status_code == 200
    with httpbin.get("base64/test") as response:
        assert response.status_code in (200, 301, 302)


FIREFOX_USER_AGENT = (
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0"
)
FIREFOX_HEADERS = {"User-Agent": FIREFOX_USER_AGENT}


def check_user_agent_header(ua_header, ua_header_ref):
    """
    Unlike original httpbin, httpbingo.org sends back a list of header
    strings instead of a simple string. So we need to be a bit flexible
    with the answer.
    """
    if isinstance(ua_header, list):
        assert len(ua_header) == 1
        assert ua_header[0] == ua_header_ref
        return
    assert ua_header == ua_header_ref


@pytest.mark.network
def test_client_with_default_headers():
    httpbin = httpbin_client(headers=FIREFOX_HEADERS)
    response = httpbin.get("/headers")
    assert response.status_code == 200
    sent_headers = json.loads(response.read().decode())["headers"]
    check_user_agent_header(sent_headers["User-Agent"], FIREFOX_USER_AGENT)


@pytest.mark.network
def test_request_with_headers(httpbin):
    response = httpbin.get("/headers", headers=FIREFOX_HEADERS)
    assert response.status_code == 200
    sent_headers = json.loads(response.read().decode())["headers"]
    check_user_agent_header(sent_headers["User-Agent"], FIREFOX_USER_AGENT)


@pytest.mark.network
def test_response_context_manager(httpbin):
    r = None
    with httpbin.get("/") as response:
        assert response.status_code == 200
        r = response
    assert r._sock is None  # released


@pytest.mark.network
def test_multi_queries_greenlet_safe():
    httpbin = httpbin_client(concurrency=3)
    group = gevent.pool.Group()
    event = gevent.event.Event()

    def run(i):
        event.wait()
        response = httpbin.get("/")
        return response, response.read()

    count = 0
    ok_count = 0

    gevent.spawn_later(0.2, event.set)
    for response, content in group.imap_unordered(run, range(5)):
        # occasionally remotely hosted httpbin does return server errors :-/
        assert response.status_code in {200, 502, 504}
        if response.status_code == 200:
            ok_count += 1
        assert len(content)
        count += 1
    assert count == 5
    # ensure at least 3 of requests got 200
    assert ok_count >= 3
