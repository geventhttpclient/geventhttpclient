import errno
from geventhttpclient.connectionpool import ConnectionPool, SSLConnectionPool
from geventhttpclient.response import HTTPSocketPoolResponse
from geventhttpclient.url import URL
from geventhttpclient import __version__
import gevent.socket
import gevent.pool


class HTTPClient(object):

    HTTP_11 = 'HTTP/1.1'
    HTTP_10 = 'HTTP/1.0'

    BLOCK_SIZE = 1024 * 4 # 4KB
    CONNECTION_TIMEOUT = 10
    NETWORK_TIMEOUT = 10

    DEFAULT_HEADERS = {
        'User-Agent': 'python/gevent-http-client-' + __version__
    }

    @staticmethod
    def from_url(url, **kw):
        if not isinstance(url, URL):
            url = URL(url)
        enable_ssl = url.scheme == 'https'
        return HTTPClient(url.host, port=url.port, ssl=enable_ssl, **kw)

    def __init__(self, host, port=None, headers={},
            block_size=BLOCK_SIZE, connection_timeout=None,
            network_timeout=None, disable_ipv6=False,
            concurrency=1, ssl_options=None, ssl=False,
            proxy_host=None, proxy_port=None, version=HTTP_11):
        self.host = host
        self.port = port
        connection_host = self.host
        connection_port = self.port
        if proxy_host is not None:
            assert proxy_port is not None, \
                'you have to provide proxy_port if you set a proxy_host'
            self.use_proxy = True
            connection_host = self.proxy_host
            connection_port = self.proxy_port
        else:
            self.use_proxy = False
        if ssl and ssl_options is None:
            ssl_options = {}
        if ssl_options is not None:
            self.ssl = True
            self.port = connection_port = self.port or 443
            self._connection_pool = SSLConnectionPool(
                connection_host, connection_port, size=concurrency,
                ssl_options=ssl_options,
                network_timeout=network_timeout,
                connection_timeout=connection_timeout,
                disable_ipv6=disable_ipv6)
        else:
            self.ssl = False
            self.port = connection_port = self.port or 80
            self._connection_pool = ConnectionPool(
                connection_host, connection_port,
                size=concurrency,
                network_timeout=network_timeout,
                connection_timeout=connection_timeout,
                disable_ipv6=disable_ipv6)
        self.version = version
        self.default_headers = self.DEFAULT_HEADERS.copy()
        for field, value in headers.iteritems():
            self.default_headers[field] = value

        self.block_size = block_size
        self._base_url_string = str(self.get_base_url())

    def get_base_url(self):
        url = URL()
        url.host = self.host
        url.port = self.port
        url.scheme = self.ssl and 'https' or 'http'
        return url

    def close(self):
        self._connection_pool.close()

    def _build_request(self, method, query_string, body="", headers={}):
        header_fields = self.default_headers.copy()
        for field, value in headers.iteritems():
            header_fields[field] = value
        if self.version == self.HTTP_11 and 'Host' not in header_fields:
            host_port = self.host
            if self.port not in (80, 443):
                host_port += ":" + str(self.port)
            header_fields['Host'] = host_port
        if body:
            header_fields['Content-Length'] = str(len(body))

        request_url = query_string
        if self.use_proxy:
            base_url = self._base_url_string
            if query_string.startswith('/'):
                base_url = base_url[:-1]
            request_url = base_url + query_string
        request = method + " " + request_url + " " + self.version + "\r\n"

        for field, value in header_fields.iteritems():
            request += str(field) + ': ' + value + "\r\n"
        request += "\r\n"
        if body:
            request += body
        return request

    def _send_request(self, request, max_reset=None):
        """ send request to the server and return socket used.
        """
        sent = 0
        reset_count = 0
        max_reset = max_reset or self._connection_pool.size

        while True:
            sock = self._connection_pool.get_socket()
            try:
                sent = sock.send(request)
                if sent != len(request):
                    sock.sendall(request[sent:])
                return sock
            except gevent.socket.error as e:
                self._connection_pool.release_socket(sock)
                if e.errno == errno.ECONNRESET and max_reset < reset_count:
                    reset_count += 1
                    continue
                raise e

    def request(self, method, query_string, body=b"", headers={}):
        request = self._build_request(
            method.upper(), query_string, body=body, headers=headers)
        sock = self._send_request(request)
        response = HTTPSocketPoolResponse(sock, self._connection_pool,
            block_size=self.block_size, method=method.upper())
        return response

    def get(self, query_string, headers={}):
        return self.request('GET', query_string, headers=headers)

    def post(self, query_string, body=u'', headers={}):
        return self.request('POST', query_string, body=body, headers=headers)

    def put(self, query_string, body=u'', headers={}):
        return self.request('PUT', query_string, body=body, headers=headers)

    def delete(self, query_string, body=u'', headers={}):
        return self.request('DELETE', query_string, body=body, headers=headers)


