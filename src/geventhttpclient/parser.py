from __future__ import absolute_import
import sys
import imp
import os.path

if '__pypy__' not in sys.builtin_module_names:
    from geventhttpclient._parser import *

else:
    # Alternative CFFI interface for use with PyPy
    import httplib
    from cffi import FFI
    ffi = FFI()
    ffi.cdef("""
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

enum http_parser_type { HTTP_REQUEST, HTTP_RESPONSE, HTTP_BOTH };

void http_parser_init(http_parser *parser, enum http_parser_type type);

size_t http_parser_execute(http_parser *parser,
                           const http_parser_settings *settings,
                           const char *data,
                           size_t len);

int http_should_keep_alive(http_parser *parser);

const char *http_errno_description(enum http_errno err);
""")

    from geventhttpclient import __path__ as mod_path
    mod_name = '_cffi__parser_helper'
    (mod_fd, mod_file_name, mod_desc) = imp.find_module(mod_name, mod_path)
    if mod_desc[2] == imp.C_EXTENSION:
        C = ffi.dlopen(mod_file_name)
    else:
        raise ImportError("Native '{}' component library could not be found. Check your installation.".format(mod_name))

    del mod_path, mod_name, mod_fd, mod_file_name, mod_desc

    class HTTPParseError(httplib.HTTPException):
        def __init__(self, http_errno):
            args = (ffi.string(C.http_errno_description(http_errno)), http_errno)
            super(HTTPParseError, self).__init__(self, args)

    class HTTPResponseParser(object):
        def _make_callback(self, name, has_args):
            base_name = '_on_' + name
            def real_cb(http_parser, buf=None, size=None):
                base_callback = getattr(self, base_name, None)
                if not base_callback:
                    return 0
                try:
                    if has_args:
                        ret = base_callback(ffi.buffer(buf, size)[:])
                    else:
                        ret = base_callback()
                    ret = 1 if ret else 0
                except:
                    self._exception = sys.exc_info()[1]
                    ret = 1
                return ret

            func_type = "http_data_cb" if has_args else "http_cb"
            return ffi.callback(func_type, real_cb)
        
        def __init__(self):
            self.http_parser = ffi.new("http_parser *")
            C.http_parser_init(self.http_parser, C.HTTP_RESPONSE)

            self.http_parser_settings = ffi.new("http_parser_settings *")
            callbacks = []

            for cb_params in (('message_begin', False),
                              #('url', True),
                              ('header_field', True),
                              ('header_value', True),
                              ('headers_complete', False),
                              ('body', True),
                              ('message_complete', False)):
                wrapped = self._make_callback(*cb_params)
                callbacks.append(wrapped)
                setattr(self.http_parser_settings, 'on_' + cb_params[0], wrapped)

            # Keep this list so wrapped callbacks are not GC'ed.
            self._callbacks = callbacks

        def feed(self, data):
            buf = ffi.new("char[]", data)
            self._exception = None
            ret = C.http_parser_execute(self.http_parser, self.http_parser_settings,
                                        buf, len(data));

            if self._exception:
                raise self._exception
            if self.parser_failed():
                raise HTTPParseError(self.http_parser.http_errno)

            return ret

        def get_code(self):
            return self.http_parser.status_code

        def get_http_version(self):
            return 'HTTP/{}.{}'.format(self.http_parser.http_major, self.http_parser.http_minor);

        def get_remaining_content_length(self):
            return self.http_parser.content_length

        def parser_failed(self):
            return self.http_parser.http_errno != 0

        def should_keep_alive(self):
            return C.http_should_keep_alive(self.http_parser)
