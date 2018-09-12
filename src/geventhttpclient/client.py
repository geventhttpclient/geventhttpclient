import six
import errno
import os
import functools
from geventhttpclient.connectionpool import ConnectionPool
from geventhttpclient.response import HTTPSocketPoolResponse
from geventhttpclient.response import HTTPConnectionClosed
from geventhttpclient.url import URL
from geventhttpclient.header import Headers
from geventhttpclient import __version__
import gevent.socket

CRLF = "\r\n"
WHITESPACE = " "
FIELD_VALUE_SEP = ": "
HOST_PORT_SEP = ":"
SLASH = "/"
PROTO_HTTP = "http"
PROTO_HTTPS = "https"
HEADER_HOST = "Host"
HEADER_CONTENT_LENGTH = "Content-Length"

METHOD_GET      = "GET"
METHOD_HEAD     = "HEAD"
METHOD_POST     = "POST"
METHOD_PUT      = "PUT"
METHOD_DELETE   = "DELETE"


class HTTPClient(object):

    HTTP_11 = 'HTTP/1.1'
    HTTP_10 = 'HTTP/1.0'

    BLOCK_SIZE = 1024 * 4 # 4KB

    DEFAULT_HEADERS = Headers({
        'User-Agent': 'python/gevent-http-client-' + __version__
    })

    @classmethod
    def from_url(cls, url, **kw):
        if not isinstance(url, URL):
            url = URL(url)
        enable_ssl = url.scheme == PROTO_HTTPS
        if not enable_ssl:
            kw.pop('ssl_options', None)
        return cls(url.host, port=url.port, ssl=enable_ssl, **kw)

    def __init__(self, host, port=None, headers={},
            block_size=BLOCK_SIZE,
            connection_timeout=ConnectionPool.DEFAULT_CONNECTION_TIMEOUT,
            network_timeout=ConnectionPool.DEFAULT_NETWORK_TIMEOUT,
            disable_ipv6=False,
            concurrency=1,
            ssl=False, ssl_options=None, ssl_context_factory=None,
            insecure=False,
            proxy_host=None, proxy_port=None, version=HTTP_11,
            headers_type=Headers):
        self.host = host
        self.port = port
        connection_host = self.host
        connection_port = self.port
        if proxy_host is not None:
            assert proxy_port is not None, \
                'you have to provide proxy_port if you set proxy_host'
            self.use_proxy = True
            connection_host = proxy_host
            connection_port = proxy_port
        else:
            self.use_proxy = False
        if ssl and ssl_options is None:
            ssl_options = {}
        if ssl_options is not None:
            self.ssl = True
            if not self.port:
                self.port = 443
            if not connection_port:
                connection_port = self.port
            # Import SSL as late as possible, fail hard with Import Error
            from geventhttpclient.connectionpool import SSLConnectionPool
            self._connection_pool = SSLConnectionPool(
                connection_host, connection_port, size=concurrency,
                ssl_options=ssl_options,
                ssl_context_factory=ssl_context_factory,
                insecure=insecure,
                network_timeout=network_timeout,
                connection_timeout=connection_timeout,
                disable_ipv6=disable_ipv6)
        else:
            self.ssl = False
            if not self.port:
                self.port = 80
            if not connection_port:
                connection_port = self.port
            self._connection_pool = ConnectionPool(
                connection_host, connection_port,
                size=concurrency,
                network_timeout=network_timeout,
                connection_timeout=connection_timeout,
                disable_ipv6=disable_ipv6)
        self.version = version
        self.headers_type = headers_type
        self.default_headers = headers_type()
        self.default_headers.update(self.DEFAULT_HEADERS)
        self.default_headers.update(headers)
        self.block_size = block_size
        self._base_url_string = str(self.get_base_url())

    def get_base_url(self):
        url = URL()
        url.host = self.host
        url.port = self.port
        url.scheme = self.ssl and PROTO_HTTPS or PROTO_HTTP
        return url

    def close(self):
        self._connection_pool.close()

    def __del__(self):
        self.close()

    # Like urllib2, try to treat the body as a file if we can't determine the
    # file length with `len()`
    def _get_body_length(self, body):
        try:
            return len(body)
        except TypeError:
            try:
                return os.fstat(body.fileno()).st_size
            except (AttributeError, OSError):
                return None

    def _build_request(self, method, request_uri, body="", headers={}):
        header_fields = self.headers_type()
        header_fields.update(self.default_headers)
        header_fields.update(headers)
        if self.version == self.HTTP_11 and HEADER_HOST not in header_fields:
            host_port = self.host
            if self.port not in (80, 443):
                host_port += HOST_PORT_SEP + str(self.port)
            header_fields[HEADER_HOST] = host_port
        if body and HEADER_CONTENT_LENGTH not in header_fields:
            body_length = self._get_body_length(body)
            if body_length:
                header_fields[HEADER_CONTENT_LENGTH] = body_length

        request_url = request_uri
        if self.use_proxy:
            base_url = self._base_url_string
            if request_uri.startswith(SLASH):
                base_url = base_url[:-1]
            request_url = base_url + request_url
        elif not request_url.startswith((SLASH, PROTO_HTTP)):
            request_url = SLASH + request_url
        elif request_url.startswith(PROTO_HTTP):
            if request_url.startswith(self._base_url_string):
                request_url = request_url[len(self._base_url_string)-1:]
            else:
                raise ValueError("Invalid host in URL")

        request = method + WHITESPACE + request_url + WHITESPACE + self.version + CRLF

        for field, value in header_fields.iteritems():
            request += field + FIELD_VALUE_SEP + str(value) + CRLF
        request += CRLF
        return request

    def request(self, method, request_uri, body=b"", headers={}):
        if isinstance(body, six.text_type):
            body = body.encode('utf-8')

        request = self._build_request(
            method.upper(), request_uri, body=body, headers=headers)

        attempts_left = self._connection_pool.size + 1

        while 1:
            sock = self._connection_pool.get_socket()
            try:
                _request = request.encode()
                if body:
                    if isinstance(body, six.binary_type):
                        sock.sendall(_request + body)
                    else:
                        sock.sendall(_request)
                        # TODO: Support non file-like iterables, e.g. `(u"string1", u"string2")`.
                        if six.PY3:
                            sock.sendfile(body)
                        else:
                            while True:
                                chunk = body.read(65536)
                                if not chunk:
                                    break
                                sock.sendall(chunk)
                else:
                    sock.sendall(_request)
            except gevent.socket.error as e:
                self._connection_pool.release_socket(sock)
                if (e.errno == errno.ECONNRESET or e.errno == errno.EPIPE) and attempts_left > 0:
                    attempts_left -= 1
                    continue
                raise e

            try:
                response = HTTPSocketPoolResponse(sock, self._connection_pool,
                    block_size=self.block_size, method=method.upper(), headers_type=self.headers_type)
            except HTTPConnectionClosed as e:
                # connection is released by the response itself
                if attempts_left > 0:
                    attempts_left -= 1
                    continue
                raise e
            else:
                response._sent_request = request
                return response

    def get(self, request_uri, headers={}):
        return self.request(METHOD_GET, request_uri, headers=headers)

    def head(self, request_uri, headers={}):
        return self.request(METHOD_HEAD, request_uri, headers=headers)

    def post(self, request_uri, body=u'', headers={}):
        return self.request(METHOD_POST, request_uri, body=body, headers=headers)

    def put(self, request_uri, body=u'', headers={}):
        return self.request(METHOD_PUT, request_uri, body=body, headers=headers)

    def delete(self, request_uri, body=u'', headers={}):
        return self.request(METHOD_DELETE, request_uri, body=body, headers=headers)


class HTTPClientPool(object):
    """ Factory for maintaining a bunch of clients, one per URL"""
    # TODO: Add some housekeeping and cleanup logic
    default_pool_size = 32

    def __init__(self, pool_size: int = None, **kwargs):
        self.__client_kwargs = kwargs
        self.get_client = functools.lru_cache(
            maxsize=pool_size or self.default_pool_size)(self.get_client)

    def get_client(self, url):
        if not isinstance(url, URL):
            url = URL(url)
        return HTTPClient.from_url(url, **self.__client_kwargs)

    def close(self):
        self.get_client.cache_clear()
