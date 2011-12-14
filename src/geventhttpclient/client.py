import errno
from geventhttpclient.connectionpool import ConnectionPool, SSLConnectionPool
from geventhttpclient.response import HeaderField, HTTPSocketResponse
from geventhttpclient import __version__
import gevent.socket
import gevent.pool


class HTTPClient(object):

    HTTP_11 = 'HTTP/1.1'
    HTTP_10 = 'HTTP/1.0'

    CHUNK_SIZE = 1024 * 16 # 16KB
    CONNECTION_TIMEOUT = 10
    NETWORK_TIMEOUT = 10

    DEFAULT_HEADERS = {
        HeaderField('User-Agent'): 'python/gevent-http-client-' + __version__,
    }

    def __init__(self, host, port, headers={},
            chunk_size=None, connection_timeout=None,
            network_timeout=None, disable_ipv6=False,
            connection_count=5, ssl_options=None):
        self._host = host
        self._port = port
        if ssl_options is not None:
            self._connection_pool = SSLConnectionPool(
                self._host, self._port, size=connection_count,
                ssl_options=ssl_options)
        else:
            self._connection_pool = ConnectionPool(
                self._host, self._port, size=connection_count)
        self.version = self.HTTP_11
        self._socket = None
        self._pool = gevent.pool.Pool(1)
        self.default_headers = self.DEFAULT_HEADERS.copy()
        for field, value in headers.iteritems():
            self.default_headers[HeaderField(field)] = value

        self.chunk_size = chunk_size or self.CHUNK_SIZE
        self._sock = None

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except:
                pass
            finally:
                self._sock = None

    def _build_request(self, method, query_string, body="", headers={}):
        header_fields = self.default_headers.copy()
        for field, value in headers.iteritems():
            header_fields[HeaderField(field)] = value
        if self.version == self.HTTP_11 and 'Host' not in header_fields:
            header_fields[HeaderField('Host')] = \
                    self._host + ":" + str(self._port)
        if body:
            header_fields[HeaderField('Content-Length'), len(body)]

        request = method + " " + query_string + " " + self.version + "\r\n"
        for field, value in header_fields.iteritems():
            request += str(field) + ': ' + value + "\r\n"
        request += "\r\n"
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
            else:
                break

    def request(self, method, query_string, body=b"", headers={}, retry=False):
        request = self._build_request(method.upper(), query_string, body, headers)
        sock = self._send_request(request)
        response = HTTPSocketResponse(sock, self._connection_pool,
            chunk_size=self.chunk_size, no_body=method.upper() in ('HEAD',))
        return response


