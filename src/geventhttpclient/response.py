import errno
from geventhttpclient._parser import HTTPResponseParser, HTTPParseError
import gevent.socket


HEADER_STATE_INIT = 0
HEADER_STATE_FIELD = 1
HEADER_STATE_VALUE = 2
HEADER_STATE_DONE = 3


class HTTPResponse(HTTPResponseParser):

    def __init__(self, method='GET'):
        super(HTTPResponse, self).__init__()
        self.method = method.upper()
        self.headers_complete = False
        self.message_begun = False
        self.message_complete = False
        self._headers_index = {}
        self._header_state = HEADER_STATE_INIT
        self._current_header_field = None
        self._current_header_value = None
        self._header_position = 1
        self._dirty = False
        self.has_body = False
        self._body_buffer = bytearray()

    def __getitem__(self, key):
        return self._headers_index[key.lower()]

    def get(self, key, default=None):
        return self._headers_index.get(key.lower(), default)

    def iteritems(self):
        for field in self._headers_index.keys():
            yield (field, self._headers_index[field])

    def items(self):
        return list(self.iteritems())

    def should_keep_alive(self):
        """ return if the headers instruct to keep the connection
        alive.
        """
        return bool(super(HTTPResponse, self).should_keep_alive())

    def should_close(self):
        """ return if we should close the connection.

        It is not the opposite of should_keep_alive method. It also checks
        that the body as been consumed completely and that the socket is not
        in a "dirty" state.
        """
        return self._dirty or \
            not self.message_complete or \
            not super(HTTPResponse, self).should_keep_alive()

    headers = property(items)

    def __contains__(self, key):
        return key in self._headers_index

    @property
    def status_code(self):
        return self.get_code()

    @property
    def content_length(self):
        length = self.get('content-length', None)
        if length is not None:
            return long(length)

    @property
    def version(self):
        return self.get_http_version()

    def _on_message_begin(self):
        if self.message_begun:
            # stop the parser we have a new response
            return True
        self.message_begun = True

    def _on_message_complete(self):
        self.message_complete = True

    def _on_headers_complete(self):
        self._flush_header()
        self._header_state = HEADER_STATE_DONE
        self.headers_complete = True

        # http://www.w3.org/Protocols/rfc2616/rfc2616-sec4.html#sec4.4
        # return True if the response doesn't or shouldn't have a body
        # this instruct the parser to skip it and to consider the message
        # complete.
        if self.get_remaining_content_length() < 0 and \
                self.get('Transfer-Encoding', 'identity') is 'identity':
            if self.should_keep_alive():
                return True
            else:
                self.has_body = True
                return False
        elif self.method in ('HEAD',) or \
                self.status_code / 100 == 1 or \
                self.status_code in (204, 304):
            # a body is present but the rfc forbids it
            # the connection will be closed after the request
            self._dirty = True
            return True
        self.has_body = True
        return False

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
            self._headers_index[self._current_header_field.lower()] = \
                self._current_header_value
            self._header_position += 1
            self._current_header_field = None
            self._current_header_value = None

    def _on_body(self, buf):
        self._body_buffer += buf


class HTTPSocketResponse(HTTPResponse):

    DEFAULT_BLOCK_SIZE = 1024 * 4 # 4KB

    def __init__(self, sock, block_size=DEFAULT_BLOCK_SIZE,
            method='GET'):
        super(HTTPSocketResponse, self).__init__(method=method)
        self._sock = sock
        self.block_size = block_size
        self._read_headers()

    def release(self):
        try:
            if self._sock is not None and self.should_close():
                try:
                    self._sock.close()
                except:
                    pass
        finally:
            self._sock = None

    def __del__(self):
        self.release()

    def _read_headers(self):
        try:
            while not self.headers_complete:
                try:
                    data = self._sock.recv(self.block_size)
                    self.feed(data)
                    # depending on gevent version we get a conn reset or no data
                    if not len(data) and not self.headers_complete:
                        raise HTTPParseError('connection closed before'
                                            ' end of the headers')
                except gevent.socket.error as e:
                    if e.errno == errno.ECONNRESET:
                        raise HTTPParseError('connection closed before'
                                             ' end of the headers')
                    raise

            if self.message_complete:
                self.release()
        except:
            self.release()
            raise

    def readline(self, sep="\r\n"):
        cursor = 0
        while True:
            cursor = self._body_buffer.find(sep, cursor)
            if cursor >= 0:
                length = cursor + len(sep)
                line = str(self._body_buffer[:length])
                del self._body_buffer[:length]
                cursor = 0
                return line
            else:
                cursor = len(self._body_buffer)
            if self.message_complete:
                return ''
            try:
                data = self._sock.recv(self.block_size)
                self.feed(data)
            except:
                self.release()
                raise

    def read(self, length=None):
        # get the existing body that may have already been parsed
        # during headers parsing
        if length is not None and len(self._body_buffer) >= length:
            read = self._body_buffer[0:length]
            del self._body_buffer[0:length]
            return str(read)

        if self._sock is None:
            read = str(self._body_buffer)
            del self._body_buffer[:]
            return read

        try:
            while not(self.message_complete) and (
                    length is None or len(self._body_buffer) < length):
                data = self._sock.recv(length or self.block_size)
                self.feed(data)
        except:
            self.release()
            raise

        if length is not None:
            read = str(self._body_buffer[0:length])
            del self._body_buffer[0:length]
            return read

        read = str(self._body_buffer)
        del self._body_buffer[:]
        return read

    def __iter__(self):
        return self

    def next(self):
        bytes = self.read(self.block_size)
        if not len(bytes):
            raise StopIteration()
        return bytes

    def _on_message_complete(self):
        super(HTTPSocketResponse, self)._on_message_complete()
        self.release()


class HTTPSocketPoolResponse(HTTPSocketResponse):

    def __init__(self, sock, pool, **kw):
        self._pool = pool
        super(HTTPSocketPoolResponse, self).__init__(sock, **kw)

    def release(self):
        try:
            if self._sock is not None:
                if self.should_close():
                    self._pool.release_socket(self._sock)
                else:
                    self._pool.return_socket(self._sock)
        finally:
            self._sock = None
            self._pool = None

    def __del__(self):
        if self._sock is not None:
            self._pool.release_socket(self._sock)

