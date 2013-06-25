from __future__ import absolute_import
import sys
import imp
import os.path

if '__pypy__' not in sys.builtin_module_names:
    from geventhttpclient._parser import *
    print 'Using native lib.'

else:
    print 'Using CFFI lib.'
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

size_t http_parser_execute(http_parser *parser,
                           const http_parser_settings *settings,
                           const char *data,
                           size_t len);

int http_should_keep_alive(http_parser *parser);

const char *http_errno_description(enum http_errno err);
""")

    file_suffix = '.so'
    for (s, m, t) in imp.get_suffixes():
        if t == imp.C_EXTENSION:
            file_suffix = s
            break

    module_abs_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), '_parser{}'.format(file_suffix))
    C = ffi.dlopen(module_abs_path)
    del file_suffix, module_abs_path

    class HTTPParseError(httplib.HTTPException):
        def __init__(self, http_errno):
            args = (ffi.string(C.http_errno_description(http_errno)), http_errno)
            super(HTTPParseError, self).__init__(self, args)

    class HTTPResponseParser(object):
        def __init__(self):
            self.http_parser = ffi.new("http_parser *")
            self.http_parser_settings = ffi.new("http_parser_settings *")
            self._callbacks = {}

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

    # Callback property, used to define behavior on setting/deleting 
    class CB(object):
        def __init__(self, name, has_args):
            self.name = 'on_' + name
            self.has_args = has_args

        def __get__(self, instance, owner):
            cb_data = instance._callbacks.get(self.name)
            if cb_data:
                return cb_data[0] # Base function
            return None

        def __set__(self, instance, base_callback):
            if base_callback is None:
                wrapped = ffi.NULL
            elif self.has_args:
                def real_cb(http_parser, buf, size):
                    return base_callback(ffi.buffer(buf, size)[:])
                wrapped = ffi.callback("http_data_cb", real_cb)
            else:
                def real_cb(http_parser):
                    return base_callback()
                wrapped = ffi.callback("http_cb", real_cb)

            instance._base_cbs[self.name] = (value, wrapped)
            setattr(instance.http_parser_settings, self.name, wrapped)

        def __delete__(self, instance):
            setattr(instance.http_parser_settings, self.name, ffi.NULL)
            del instance._base_cbs[self.name]

    for (callback, has_args) in (('message_begin', False),
                                 #('url', True),
                                 ('header_field', True),
                                 ('header_value', True),
                                 ('headers_complete', False),
                                 ('body', True),
                                 ('message_complete', False)):
        setattr(HTTPResponseParser, '_on_' + callback, CB(callback, has_args))
