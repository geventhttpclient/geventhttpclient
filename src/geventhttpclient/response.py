import errno
from collections.abc import Iterator
from typing import Any, Self

import gevent.socket

from geventhttpclient._parser import HTTPParseError, HTTPResponseParser
from geventhttpclient.connectionpool import ConnectionPool
from geventhttpclient.header import Headers

HEADER_STATE_INIT = 0
HEADER_STATE_FIELD = 1
HEADER_STATE_VALUE = 2
HEADER_STATE_DONE = 3


def copy(data: bytes | bytearray) -> bytes:
    return bytes(data)


class HTTPConnectionClosed(HTTPParseError):
    pass


class HTTPProtocolViolationError(HTTPParseError):
    pass


class HTTPResponse(HTTPResponseParser):
    def __init__(self, method: str = "GET", headers_type: type[Headers] = Headers) -> None:
        super().__init__()
        self.method = method.upper()
        self.headers_complete = False
        self.message_begun = False
        self.message_complete = False
        self._headers_index = headers_type()
        self._header_state = HEADER_STATE_INIT
        self._current_header_field: str | None = None
        self._current_header_value: str | None = None
        self._header_position = 1
        self._body_buffer = bytearray()
        self.status_message: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self._headers_index[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._headers_index.get(key, default)

    def items(self) -> Iterator[tuple[str, Any]]:
        return self._headers_index.items()

    headers = property(items)

    def info(self) -> Headers:
        # compatibility with http.client
        return self._headers_index

    def __contains__(self, key: str) -> bool:
        return key in self._headers_index

    def should_close(self) -> bool:
        """return if we should close the connection.

        It is not the opposite of should_keep_alive method. It also checks
        that the body as been consumed completely.
        """
        return not self.message_complete or self.parser_failed() or not super().should_keep_alive()

    @property
    def status_code(self) -> int:
        return self.get_code()

    @property
    def content_length(self) -> int | None:
        length = self.get("content-length", None)
        if length is not None:
            return int(length)
        return None

    @property
    def length(self) -> int | None:
        return self.content_length

    @property
    def version(self) -> str:
        return self.get_http_version()

    def _on_status(self, msg: str) -> None:
        self.status_message = msg

    def _on_message_begin(self) -> None:
        if self.message_begun and not self.message_complete:
            raise HTTPProtocolViolationError(f"A new response began before end of {self!r}.")
        if self.message_complete:
            # A complete bodyless message (e.g. an interim 1xx response) is
            # followed by the final response on the same parser and socket.
            self.headers_complete = False
            self.message_complete = False
            self._headers_index.clear()
            self._header_state = HEADER_STATE_INIT
            self._current_header_field = None
            self._current_header_value = None
        self.message_begun = True

    def _on_message_complete(self) -> None:
        self.message_complete = True

    def _on_headers_complete(self) -> int | bool | None:
        self._flush_header()
        self._header_state = HEADER_STATE_DONE
        self.headers_complete = True

        return self.method == "HEAD"  # SKIP BODY

    def _on_header_field(self, string: str) -> None:
        if self._header_state == HEADER_STATE_FIELD:
            self._current_header_field = (self._current_header_field or "") + string
        else:
            if self._header_state == HEADER_STATE_VALUE:
                self._flush_header()
            self._current_header_field = string

        self._header_state = HEADER_STATE_FIELD

    def _on_header_value(self, string: str) -> None:
        if self._header_state == HEADER_STATE_VALUE:
            self._current_header_value = (self._current_header_value or "") + string
        else:
            self._current_header_value = string

        self._header_state = HEADER_STATE_VALUE

    def _flush_header(self) -> None:
        if self._current_header_field is not None:
            self._headers_index.add(self._current_header_field, self._current_header_value)
            self._header_position += 1
            self._current_header_field = None
            self._current_header_value = None

    def _on_body(self, buf: bytearray) -> None:
        self._body_buffer += buf

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} status={self.status_code} headers={dict(self.headers)}>"


class HTTPSocketResponse(HTTPResponse):
    DEFAULT_BLOCK_SIZE = 1024 * 4  # 4KB

    def __init__(
        self,
        sock: gevent.socket.socket,
        block_size: int = DEFAULT_BLOCK_SIZE,
        method: str = "GET",
        headers_type: type[Headers] = Headers,
        pre_buffered: bytes = b"",
        **kw: Any,
    ) -> None:
        super().__init__(method=method, headers_type=headers_type)
        self._sock: gevent.socket.socket | None = sock
        self.block_size = block_size
        self._read_headers(pre_buffered)

    def release(self) -> None:
        try:
            if self._sock is not None and self.should_close():
                try:
                    self._sock.close()
                except:  # noqa
                    pass
        finally:
            self._sock = None

    def __del__(self) -> None:
        self.release()

    def _read_headers(self, pre_buffered: bytes = b"") -> None:
        sock = self._sock
        assert sock is not None
        try:
            if pre_buffered:
                self.feed(pre_buffered)
                start = False
            else:
                start = True
            # Interim 1xx responses are bodyless and followed by the final
            # response on the same connection. Keep reading until the headers
            # of a final (non 1xx) response are complete.
            while not self.headers_complete or self.get_code() < 200:
                try:
                    data = sock.recv(self.block_size)
                    self.feed(data)
                    # depending on gevent version we get a conn reset or no data
                    if not len(data):
                        if start:
                            raise HTTPConnectionClosed("connection closed.")
                        if self.headers_complete:
                            raise HTTPParseError("connection closed after interim response")
                        raise HTTPParseError("connection closed before end of the headers")
                    start = False
                except gevent.socket.error as e:
                    if e.errno == errno.ECONNRESET and start:
                        raise HTTPConnectionClosed("connection closed.")
                    raise

            if self.message_complete:
                self.release()
        except BaseException:
            self.release()
            raise

    def readline(self, sep: bytes = b"\r\n") -> bytes:
        cursor = 0
        multibyte = len(sep) > 1
        while True:
            cursor = self._body_buffer.find(sep[0:1], cursor)
            if cursor >= 0:
                found = True
                if multibyte:
                    pos = cursor
                    cursor = self._body_buffer.find(sep, cursor)
                    if cursor < 0:
                        cursor = pos
                        found = False
                if found:
                    length = cursor + len(sep)
                    line = copy(self._body_buffer[:length])
                    del self._body_buffer[:length]
                    cursor = 0
                    return line
            else:
                cursor = 0
            if self.message_complete:
                return b""
            sock = self._sock
            if sock is None:
                raise HTTPConnectionClosed("connection closed.")
            try:
                data = sock.recv(self.block_size)
                self.feed(data)
            except BaseException:
                self.release()
                raise

    def read(self, length: int | None = None) -> bytes:
        # get the existing body that may have already been parsed
        # during headers parsing
        if length is not None and len(self._body_buffer) >= length:
            read = copy(self._body_buffer[0:length])
            del self._body_buffer[0:length]
            return read

        if self._sock is None:
            read = copy(self._body_buffer)
            del self._body_buffer[:]
            return read

        sock = self._sock
        try:
            while not self.message_complete and (length is None or len(self._body_buffer) < length):
                data = sock.recv(length or self.block_size)
                self.feed(data)
        except:
            self.release()
            raise

        if length is not None:
            read = copy(self._body_buffer[0:length])
            del self._body_buffer[0:length]
            return read

        read = copy(self._body_buffer)
        del self._body_buffer[:]
        return read

    def __iter__(self) -> "HTTPSocketResponse":
        return self

    def __next__(self) -> bytes:
        data = self.read(self.block_size)
        if not len(data):
            raise StopIteration()
        return data

    def _on_message_complete(self) -> None:
        super()._on_message_complete()
        self.release()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class HTTPSocketPoolResponse(HTTPSocketResponse):
    def __init__(self, sock: gevent.socket.socket, pool: ConnectionPool, **kw: Any) -> None:
        self._pool: ConnectionPool | None = pool
        super().__init__(sock, **kw)

    def release(self) -> None:
        pool, sock = self._pool, self._sock
        try:
            if sock is not None and pool is not None:
                if self.should_close():
                    pool.release_socket(sock)
                else:
                    pool.return_socket(sock)
        finally:
            self._sock = None
            self._pool = None

    def __del__(self) -> None:
        if self._sock is not None and self._pool is not None:
            self._pool.release_socket(self._sock)
