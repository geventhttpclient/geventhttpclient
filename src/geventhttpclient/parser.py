import sys

if '__pypy__' not in sys.builtin_module_names:
    from geventhttpclient._parser import *

else:
    # Alternative CFFI interface for use with PyPy
    from httplib import HTTPException
    from cffi import FFI
    ffi = FFI.cdef("""

typedef struct {
  /** PRIVATE **/
  unsigned char type : 2;     /* enum http_parser_type */
  unsigned char flags : 6;    /* F_* values from 'flags' enum; semi-public */
  unsigned char state;        /* enum state from http_parser.c */
  unsigned char header_state; /* enum header_state from http_parser.c */
  unsigned char index;        /* index into current matcher */

  uint32_t nread;          /* # bytes read in various scenarios */
  uint64_t content_length; /* # bytes in body (0 if no Content-Length header) */

  /** READ-ONLY **/
  unsigned short http_major;
  unsigned short http_minor;
  unsigned short status_code; /* responses only */
  unsigned char method;       /* requests only */
  unsigned char http_errno : 7;

  /* 1 = Upgrade header was present and the parser has exited because of that.
   * 0 = No upgrade header present.
   * Should be checked when http_parser_execute() returns in addition to
   * error checking.
   */
  unsigned char upgrade : 1;

  /** PUBLIC **/
  void *data; /* A pointer to get hook to the "connection" or "socket" object */
} http_parser;

typedef int (*http_data_cb) (http_parser*, const char *at, size_t length);
typedef int (*http_cb) (http_parser*);

typedef struct {
  http_cb      on_message_begin;
  http_data_cb on_url;
  http_data_cb on_header_field;
  http_data_cb on_header_value;
  http_cb      on_headers_complete;
  http_data_cb on_body;
  http_cb      on_message_complete;
} http_parser_settings;

size_t http_parser_execute(http_parser *parser,
                           const http_parser_settings *settings,
                           const char *data,
                           size_t len);
""")
    C = ffi.dlopen('_parser') 

    class HTTPParseError(HTTPException):
        pass
    
    class HTTPResponseParser(object):
        def __init__(self):
            self.http_parser = ffi.new("http_parser *")
            self.http_parser_settings = ffi.new("http_parser_settings *")

        def feed(self, data):
            buf = ffi.new("char[]", data)
            self._exception = None
            ret = C.http_parser_execute(self.http_parser, self.http_parser_settings,
                                        buf, len(data));
            if self._exception:
                raise self._exception

            return ret

        def get_code(self):
            pass

        def get_http_version(self):
            pass

        def get_remaining_content_length(self):
            pass

        def parser_failed(self):
            pass

        def should_keep_alive(self):
            pass

