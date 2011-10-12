from httpparser._parser import HTTPResponseParser
import errno
import sys
import gevent.queue
import gevent.event
from gevent import socket


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


HEADER_STATE_INIT = 0
HEADER_STATE_FIELD = 1
HEADER_STATE_VALUE = 2
HEADER_STATE_DONE = 3


class Client(object):

    HTTP_11 = 'HTTP/1.1'
    HTTP_10 = 'HTTP/1.0'

    CHUNK_SIZE = 1024 * 16 # 16KB
    CONNECTION_TIMEOUT = 10
    NETWORK_TIMEOUT = 10

    DEFAULT_FIELDS = {
        HeaderField('User-Agent'): 'gevent-http-1.1',
    }

    def __init__(self, host, port):
        self.__host = host
        self.__port = port
        self.version = self.HTTP_11
        self.__socket = None
        self.__ready_event = gevent.event.Event()
        self.__ready_event.set()

        self.chunk_size = self.CHUNK_SIZE
        self.connection_timeout = self.CONNECTION_TIMEOUT
        self.disable_ipv6 = False

        family = 0
        if self.disable_ipv6:
            family = socket.AF_INET

        info = socket.getaddrinfo(self.__host, self.__port,
                family, 0, socket.SOL_TCP)

        family, socktype, proto, canonname, sockaddr = info[0]

        self.sock_family = family
        self.sock_type = socktype
        self.sock_protocol  = proto
        self.sock_address = sockaddr

        self._sock = None

    def __copy__(self):
        return Client(self.__host, self.__port)

    def close(self):
        if self._sock is not None:
            try:
                self._sock.close()
                self._sock = None
            except socket.error:
                pass

    def connect(self):
        self.close()
        sock = socket.socket(self.sock_family, self.sock_type, self.sock_protocol)
        # sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        # sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        #with gevent.Timeout(self.connection_timeout):
        sock.connect(self.sock_address)
        self._sock = sock
        return sock

    def request(self, method, query_string, body=b"", headers={}, retry=False):
        header_fields = self.DEFAULT_FIELDS.copy()
        for field, value in headers.iteritems():
            header_fields[HeaderField(field)] = value
        if self.version == self.HTTP_11 and 'Host' not in header_fields:
            header_fields[HeaderField('Host')] = \
                    self.__host + ":" + str(self.__port)
        if body:
            header_fields[HeaderField('Content-Length'), len(body)]

        request = method + " " + query_string + " " + self.version + "\r\n"
        for field, value in header_fields.iteritems():
            request += str(field) + ': ' + value + "\r\n"
        request += "\r\n"

        self.__ready_event.wait()

        if self._sock is None:
            self.connect()

        try:

            response = Response()
            # XXX: network timeout
            sent = 0
            try:
                sent = self._sock.send(request)
            except socket.error as e:
                if e.errno == errno.EPIPE:
                    pass
                else:
                    raise
            if sent == 0:
                self.connect()
                self._sock.sendall(request)
            elif sent != len(request):
                self._sock.sendall(request[sent:])

            if body:
                self._sock.sendall(body)

            def _retry():
                self.connect()
                self._sock.sendall(request)
                if body:
                    self._sock.sendall(body)
                return self._sock.recv(self.chunk_size)

            try:
                data = self._sock.recv(self.chunk_size)
            except socket.error as e:
                if e.errno == errno.ECONNRESET:
                    if retry:
                        data = _retry()
            else:
                if len(data) == 0:
                    if retry:
                        data = _retry()

            response.feed(data)

            if not response.message_complete_event.is_set():
                job = gevent.spawn(self._read_until_eof, response)
                job.link_exception(response._read_failed)
            return response
        except:
            self.__ready_event.set()
            raise

    def _read_until_eof(self, response):
        try:
            data = True
            while data:
                if response.message_complete_event.is_set():
                    break
                data = self._sock.recv(self.chunk_size)
                response.feed(data)

            if not response.should_keep_alive():
                self.close()
        except:
            self.close()
            raise
        finally:
            self.__ready_event.set()


class Response(HTTPResponseParser):

    EOF = sys.maxint

    def __init__(self):
        self.headers_complete_event = gevent.event.Event()
        self.message_begin_event = gevent.event.Event()
        self.message_complete_event = gevent.event.Event()
        self.__body_event = gevent.event.Event()
        self.__body_queue = gevent.queue.Queue()
        self.__headers_index = {}
        self.__header_state = HEADER_STATE_INIT
        self.__current_header_field = None
        self.__current_header_value = None
        self.__header_position = 1
        self.__eof = False

    def _read_failed(self, g):
        raise g.exception

    def __getitem__(self, key):
        return self.__headers_index[key]

    def get(self, key, default=None):
        return self.__headers_index.get(key, default)

    def iteritems(self):
        for field in sorted(self.__headers_index.keys()):
            yield (str(field), self.__headers_index[field])

    def items(self):
        return list(self.iteritems())

    def __contains__(self, key):
        return key in self.__headers_index

    def __iter__(self):
        return self

    def next(self):
        if self.__eof:
            raise StopIteration
        self.__body_event.wait()
        data = self.__body_queue.get()
        if data == self.EOF:
            self.__eof = True
            raise StopIteration
        return data

    @property
    def body(self):
        if hasattr(self, '_body'):
            return self._body
        buf = b""
        for chunk in self:
            buf += chunk
        self._body = buf
        return buf

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
        self.message_begin_event.set()

    def _on_message_complete(self):
        self.message_complete_event.set()
        self.__body_event.set()
        self.__body_queue.put(self.EOF)

    def _on_headers_complete(self):
        self.__flush_header()
        self.__header_state = HEADER_STATE_DONE
        self.headers_complete_event.set()

    def _on_header_field(self, string):
        if self.__header_state == HEADER_STATE_FIELD:
            self.__current_header_field += string
        else:
            if self.__header_state == HEADER_STATE_VALUE:
                self.__flush_header()
            self.__current_header_field = string

        self.__header_state = HEADER_STATE_FIELD

    def _on_header_value(self, string):
        if self.__header_state == HEADER_STATE_VALUE:
            self.__current_header_value += string
        else:
            self.__current_header_value = string

        self.__header_state = HEADER_STATE_VALUE

    def _on_body(self, string):
        if not self.__body_event.is_set():
            self.__body_event.set()
        self.__body_queue.put(string)

    def __flush_header(self):
        field = HeaderField(self.__current_header_field, self.__header_position)
        self.__headers_index[field] = self.__current_header_value
        self.__header_position += 1
        self.__current_header_field = None
        self.__current_header_value = None


if __name__ == "__main__":
    c = Client('127.0.0.1', 8000)
    c.request("GET", "/")
    print "wait"
    # gevent.sleep(10)
    c.request("GET", "/")

