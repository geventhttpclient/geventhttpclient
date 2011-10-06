from httpparser._parser import HTTPResponseParser
from cStringIO import StringIO

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
    assert len(parser.body)
    print parser.headers

