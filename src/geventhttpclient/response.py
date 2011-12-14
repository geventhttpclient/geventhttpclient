from geventhttpclient._parser import HTTPResponseParser


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


