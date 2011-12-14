import os
from httpparser._parser import HTTPResponseParser
import errno
import gevent.queue
import gevent.event
import gevent.coros
import gevent.pool
import gevent.ssl
from gevent import socket


from gauss.common.debug import debugger

CA_CERTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cacert.pem")


class HeaderField(object):

    def __init__(self, string, pos=0):
        self.pos = pos
        self._string = string
        self._lower = string.lower()

    def __hash__(self):
        return hash(self._lower)

    def __lt__(self, other):
        return self.pos < other.pos

    def __eq__(self, other):
        if isinstance(other, basestring):
            return self._lower == other.lower()
        return self._lower == other._lower

    def __str__(self):
        return self._string

    def __repr__(self):
        return repr(self._string)


class ConnectionPool(object):

    DEFAULT_CONNECTION_TIMEOUT = 5
    DEFAULT_NETWORK_TIMEOUT = 5

    def __init__(self, host, port,
            size=5, disable_ipv6=False,
            connection_timeout=DEFAULT_CONNECTION_TIMEOUT,
            network_timeout=DEFAULT_NETWORK_TIMEOUT):
        self._host = host
        self._port = port
        self.size = size
        self._semaphore = gevent.coros.BoundedSemaphore(size)
        self._socket_queue = gevent.queue.LifoQueue(size)
        self._connection_timeout = connection_timeout
        self.disable_ipv6 = disable_ipv6
        self._resolve()

    def _resolve(self):
        """ resolve (dns) socket informations needed to connect it.
        """
        family = 0
        if self.disable_ipv6:
            family = socket.AF_INET

        info = socket.getaddrinfo(self._host, self._port,
                family, 0, socket.SOL_TCP)

        family, socktype, proto, canonname, sockaddr = info[0]

        self._sock_family = family
        self._sock_type = socktype
        self._sock_protocol  = proto
        self._sock_address = sockaddr

    def _create_tcp_socket(self):
        """ tcp socket factory.
        """
        sock = socket.socket(self._sock_family,
            self._sock_type,
            self._sock_protocol)
        return sock

    def _create_socket(self):
        """ might be overriden and super for wrapping into a ssl socket
            or set tcp/socket options
        """
        return self._create_tcp_socket()

    def _connect_socket(self, sock):
        """ Connect a socket.
        """
        with gevent.Timeout(self._connection_timeout):
            sock.connect(self._sock_address)

    def get_socket(self):
        """ get a socket from the pool. This blocks until one is available.
        """
        self._semaphore.acquire()
        try:
            return self._socket_queue.get(block=False)
        except gevent.queue.Empty:
            sock = self._create_socket()
            self._connect_socket(sock)
            return sock

    def return_socket(self, sock):
        """ return a socket to the pool.
        """
        self._socket_queue.put(sock)
        self._semaphore.release()

    def release_socket(self, sock):
        """ call when the socket is no more usable.
        """
        try:
            sock.close()
        except:
            pass
        self._semaphore.release()


class SSLConnectionPool(ConnectionPool):

    default_options = {
        'ca_certs': CA_CERTS
    }

    def __init__(self, host, port, **kw):
        self.ssl_options = self.default_options.copy()
        self.ssl_options.update(kw['ssl_options'])
        del kw['ssl_options']
        super(SSLConnectionPool, self).__init__(host, port, **kw)

    def _create_socket(self):
        sock = super(SSLConnectionPool, self)._create_socket()
        ssl_sock = gevent.ssl.wrap_socket(sock, **self.ssl_options)
        return ssl_sock


class Client(object):

    HTTP_11 = 'HTTP/1.1'
    HTTP_10 = 'HTTP/1.0'

    CHUNK_SIZE = 1024 * 16 # 16KB
    CONNECTION_TIMEOUT = 10
    NETWORK_TIMEOUT = 10

    DEFAULT_HEADERS = {
        HeaderField('User-Agent'): 'python/gevent-http-1.1',
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

    def __copy__(self):
        return Client(self._host, self._port)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except:
                pass
            finally:
                self._sock = None

    def _build_request(self, method, query_string, body=b"", headers={}):
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
            except socket.error as e:
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


HEADER_STATE_INIT = 0
HEADER_STATE_FIELD = 1
HEADER_STATE_VALUE = 2
HEADER_STATE_DONE = 3


class HTTPResponse(HTTPResponseParser):

    def __init__(self, no_body=False):
        super(HTTPResponse, self).__init__()
        self.no_body = no_body
        self.headers_complete = False
        self.message_begun = False
        self.message_complete = False
        self._headers_index = {}
        self._header_state = HEADER_STATE_INIT
        self._current_header_field = None
        self._current_header_value = None
        self._header_position = 1

    def __getitem__(self, key):
        return self._headers_index[key]

    def get(self, key, default=None):
        return self._headers_index.get(key, default)

    def iteritems(self):
        for field in sorted(self._headers_index.keys()):
            yield (str(field), self._headers_index[field])

    def items(self):
        return list(self.iteritems())

    headers = property(items)

    def __contains__(self, key):
        return key in self._headers_index

    @property
    def status_code(self):
        return self.get_code()

    @property
    def content_length(self):
        length = self.get('content-length')
        if length is not None:
            try:
                return long(length)
            except ValueError:
                pass
        return None

    def _on_message_begin(self):
        self.message_begun = True

    def _on_message_complete(self):
        self.message_complete = True

    def _on_headers_complete(self):
        self._flush_header()
        self._header_state = HEADER_STATE_DONE
        self.headers_complete = True
        return self.no_body

    def _on_header_field(self, string):
        if self._header_state == HEADER_STATE_FIELD:
            self._current_header_field += string
        else:
            if self._header_state == HEADER_STATE_VALUE:
                self._flush_header()
            self._current_header_field = string

        self._header_state = HEADER_STATE_FIELD

    def _on_header_value(self, string):
        if self._header_state == HEADER_STATE_VALUE:
            self._current_header_value += string
        else:
            self._current_header_value = string

        self._header_state = HEADER_STATE_VALUE

    def _flush_header(self):
        if self._current_header_field is not None:
            field = HeaderField(self._current_header_field,
                    self._header_position)
            self._headers_index[field] = self._current_header_value
            self._header_position += 1
            self._current_header_field = None
            self._current_header_value = None


NO_DATA = object()


class HTTPSocketResponse(HTTPResponse):

    DEFAULT_CHUNK_SIZE = 1024 * 16 # 16KB

    def __init__(self, sock, pool, chunk_size=DEFAULT_CHUNK_SIZE,
            no_body=False):
        super(HTTPSocketResponse, self).__init__(no_body=no_body)
        self._sock = sock
        self._pool = pool
        self.chunk_size = chunk_size
        self._last_body_piece = NO_DATA
        self._read_headers()

    def release(self):
        try:
            if self._sock is not None:
                if self.should_keep_alive() and self.message_complete:
                    self._pool.return_socket(self._sock)
                else:
                    self._pool.release_socket(self._sock)
        finally:
            self._sock = None
            self._pool = None

    def __del__(self):
        if self._sock is not None:
            self._pool.release_socket(self._sock)

    def _read_headers(self):
        try:
            while not self.headers_complete:
                data = self._sock.recv(self.chunk_size)
                self.feed(data)
            if self.message_complete:
                self.release()
        except:
            self.release()
            raise

    def _on_body(self, string):
        self._last_body_piece = string

    def read_iter(self):
        try:
            if self._last_body_piece != NO_DATA:
                yield self._last_body_piece
            else:
                yield ''
            while not self.message_complete:
                data = self._sock.recv(self.chunk_size)
                self.feed(data)
                if self._last_body_piece != NO_DATA:
                    yield self._last_body_piece
                    self._last_body_piece = NO_DATA
        finally:
            self.release()

    @property
    def body(self):
        if hasattr(self, '_body'):
            return self._body
        if self.content_length == 0:
            return ""
        buf = ""
        for chunk in self.read_iter():
            buf += chunk
        self._body = buf
        return buf


if __name__ == "__main__":
    c = Client('127.0.0.1', 8000)
    c.request("GET", "/")
    print "wait"
    # gevent.sleep(10)
    c.request("GET", "/")

