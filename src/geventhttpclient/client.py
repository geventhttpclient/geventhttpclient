import errno
from geventhttpclient.connectionpool import ConnectionPool, SSLConnectionPool
from geventhttpclient.response import HTTPSocketPoolResponse
from geventhttpclient.response import HTTPConnectionClosed
from geventhttpclient.url import URL
from geventhttpclient.header import Headers
from geventhttpclient import __version__
import gevent.socket


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
        enable_ssl = url.scheme == 'https'
        if not enable_ssl:
            kw.pop('ssl_options', None)
        return cls(url.host, port=url.port, ssl=enable_ssl, **kw)

    def __init__(self, host, port=None, headers={},
            block_size=BLOCK_SIZE,
            connection_timeout=ConnectionPool.DEFAULT_CONNECTION_TIMEOUT,
            network_timeout=ConnectionPool.DEFAULT_NETWORK_TIMEOUT,
            disable_ipv6=False,
            concurrency=1, ssl_options=None, ssl=False,
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
            self._connection_pool = SSLConnectionPool(
                connection_host, connection_port, size=concurrency,
                ssl_options=ssl_options,
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
        url.scheme = self.ssl and 'https' or 'http'
        return url

    def close(self):
        self._connection_pool.close()

    def _build_request(self, method, request_uri, body="", headers={}):
        header_fields = self.headers_type()
        header_fields.update(self.default_headers)
        header_fields.update(headers)
        if self.version == self.HTTP_11 and 'Host' not in header_fields:
            host_port = self.host
            if self.port not in (80, 443):
                host_port += ":" + str(self.port)
            header_fields['Host'] = host_port
        if body and 'Content-Length' not in header_fields:
            header_fields['Content-Length'] = len(body)

        request_url = request_uri
        if self.use_proxy:
            base_url = self._base_url_string
            if request_uri.startswith('/'):
                base_url = base_url[:-1]
            request_url = base_url + request_url
        elif not request_url.startswith(('/', 'http')):
            request_url = '/' + request_url
        elif request_url.startswith('http'):
            if request_url.startswith(self._base_url_string):
                request_url = request_url[len(self._base_url_string)-1:]
            else:
                raise ValueError("Invalid host in URL")
            
        request = method + " " + request_url + " " + self.version + "\r\n"

        for field, value in header_fields.iteritems():
            request += field + ': ' + str(value) + "\r\n"
        request += "\r\n"
        if body:
            request += body
        return request

    def request(self, method, request_uri, body=b"", headers={}):
        request = self._build_request(
            method.upper(), request_uri, body=body, headers=headers)

        attempts_left = self._connection_pool.size + 1

        while 1:
            sock = self._connection_pool.get_socket()
            try:
                sock.sendall(request)
            except gevent.socket.error as e: #@UndefinedVariable
                self._connection_pool.release_socket(sock)
                if e.errno == errno.ECONNRESET and attempts_left > 0:
                    attempts_left -= 1
                    continue
                raise e

            try:
                ret = HTTPSocketPoolResponse(sock, self._connection_pool,
                    block_size=self.block_size, method=method.upper(), headers_type=self.headers_type)
            except HTTPConnectionClosed as e:
                # connection is released by the response itself
                if attempts_left > 0:
                    attempts_left -= 1
                    continue
                raise e
            else:
                ret._sent_request = request
                return ret

    def get(self, request_uri, headers={}):
        return self.request('GET', request_uri, headers=headers)

    def post(self, request_uri, body=u'', headers={}):
        return self.request('POST', request_uri, body=body, headers=headers)

    def put(self, request_uri, body=u'', headers={}):
        return self.request('PUT', request_uri, body=body, headers=headers)

    def delete(self, request_uri, body=u'', headers={}):
        return self.request('DELETE', request_uri, body=body, headers=headers)
    
    
class HTTPClientPool(object):
    """ Factory for maintaining a bunch of clients, one per host:port """
    # TODO: Add some housekeeping and cleanup logic
    
    def __init__(self, **kwargs):
        self.clients = dict()
        self.client_args = kwargs

    def get_client(self, url):
        if not isinstance(url, URL):
            url = URL(url)
        client_key = url.host, url.port
        try: 
            return self.clients[client_key]
        except KeyError:
            client = HTTPClient.from_url(url, **self.client_args)
            self.clients[client_key] = client
            return client
        
    def close(self):
        for client in self.clients.values():
            client.close()
