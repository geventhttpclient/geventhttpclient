from httpparser._parser import HTTPResponseParser
from cStringIO import StringIO

# TODO
# rewrite parser module to be able to parse either Response or Request


RESPONSE =u"""HTTP/1.1 301 Moved Permanently
Location: http://www.google.com/
Content-Type: text/html; charset=UTF-8
Date: Wed, 05 Oct 2011 23:00:34 GMT
Expires: Fri, 04 Nov 2011 23:00:34 GMT
Cache-Control: public, max-age=2592000
Server: gws
Content-Length: 219
X-XSS-Protection: 1; mode=block

<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">
<TITLE>301 Moved</TITLE></HEAD><BODY>
<H1>301 Moved</H1>
The document has moved
<A HREF="http://www.google.com/">here</A>.
</BODY></HTML>"""

HEADER_STATE_INIT = 0
HEADER_STATE_FIELD = 1
HEADER_STATE_VALUE = 2
HEADER_STATE_DONE = 3

class ResponseParser(HTTPResponseParser):

    def __init__(self):
        self.headers = {}
        self.message_begin = False
        self.message_complete = False
        self.headers_complete = False
        self.__header_state = HEADER_STATE_INIT
        self.__current_header_field = None
        self.__current_header_value = None
        self.body = ""

    @property
    def status_code(self):
        return self.get_code()

    @property
    def content_length(self):
        return self.get_content_length()

    def on_message_begin(self):
        self.message_begin = True

    def on_message_complete(self):
        self.message_complete = True

    def on_headers_complete(self):
        self.__flush_header()
        self.__current_header_field = None
        self.__current_header_value = None
        self.headers_complete = True
        self.__header_state = HEADER_STATE_DONE

    def on_header_field(self, string):
        if self.__header_state == HEADER_STATE_VALUE:
            self.__flush_header()

        if self.__header_state == HEADER_STATE_FIELD:
            self.__current_header_field += string
        else:
            self.__current_header_field = string

        self.__header_state = HEADER_STATE_FIELD

    def on_header_value(self, string):
        if self.__header_state == HEADER_STATE_VALUE:
            self.__current_header_value += string
        else:
            self.__current_header_value = string

        self.__header_state = HEADER_STATE_VALUE

    def __flush_header(self):
        self.headers[self.__current_header_field] = self.__current_header_value

    def on_body(self, string):
        self.body += string

def test_parse():
    parser = ResponseParser()
    parser.feed(RESPONSE)
    assert parser.message_begin
    assert parser.headers_complete
    assert parser.message_complete

def test_parse_chunk():
    parser = ResponseParser()
    parser.feed(RESPONSE)
    response = StringIO(RESPONSE)
    data = response.read(10)
    while data:
        parser.feed(data)
        data = response.read(10)
    assert parser.should_keep_alive()
    assert parser.status_code == 301
    assert parser.headers, {
        'Content-Length': '219',
        'X-XSS-Protection': '1; mode=block',
        'Expires': 'Fri, 04 Nov 2011 23:00:34 GMT',
        'Server': 'gws',
        'Location': 'http://www.google.com/',
        'Cache-Control': 'public, max-age=2592000',
        'Date': 'Wed, 05 Oct 2011 23:00:34 GMT',
        'Content-Type': 'text/html; charset=UTF-8'}
    assert len(parser.body) == parser.content_length

STATUS_CODES = {
  100 : 'Continue',
  101 : 'Switching Protocols',
  200 : 'OK',
  201 : 'Created',
  202 : 'Accepted',
  203 : 'Non-Authoritative Information',
  204 : 'No Content',
  205 : 'Reset Content',
  206 : 'Partial Content',
  300 : 'Multiple Choices',
  301 : 'Moved Permanently',
  302 : 'Moved Temporarily',
  303 : 'See Other',
  304 : 'Not Modified',
  305 : 'Use Proxy',
  400 : 'Bad Request',
  401 : 'Unauthorized',
  402 : 'Payment Required',
  403 : 'Forbidden',
  404 : 'Not Found',
  405 : 'Method Not Allowed',
  406 : 'Not Acceptable',
  407 : 'Proxy Authentication Required',
  408 : 'Request Time-out',
  409 : 'Conflict',
  410 : 'Gone',
  411 : 'Length Required',
  412 : 'Precondition Failed',
  413 : 'Request Entity Too Large',
  414 : 'Request-URI Too Large',
  415 : 'Unsupported Media Type',
  500 : 'Internal Server Error',
  501 : 'Not Implemented',
  502 : 'Bad Gateway',
  503 : 'Service Unavailable',
  504 : 'Gateway Time-out',
  505 : 'HTTP Version not supported'
}
