from httpparser.response import Response, Client
from httpparser._parser import HTTPParseError
from cStringIO import StringIO
import sys

RESPONSE = 'HTTP/1.1 301 Moved Permanently\r\nLocation: http://www.google.fr/\r\nContent-Type: text/html; charset=UTF-8\r\nDate: Thu, 13 Oct 2011 15:03:12 GMT\r\nExpires: Sat, 12 Nov 2011 15:03:12 GMT\r\nCache-Control: public, max-age=2592000\r\nServer: gws\r\nContent-Length: 218\r\nX-XSS-Protection: 1; mode=block\r\n\r\n<HTML><HEAD><meta http-equiv="content-type" content="text/html;charset=utf-8">\n<TITLE>301 Moved</TITLE></HEAD><BODY>\n<H1>301 Moved</H1>\nThe document has moved\n<A HREF="http://www.google.fr/">here</A>.\r\n</BODY></HTML>\r\n'

def test_refcount():
    import gc
    gc.set_debug(gc.DEBUG_LEAK)
    try:
        parser = Response()
        assert parser.feed(RESPONSE), len(RESPONSE)
        del parser
        print gc.garbage
    finally:
        gc.set_debug(0)

def test_parse():
    parser = Response()
    assert parser.feed(RESPONSE), len(RESPONSE)
    assert parser.message_begin_event.is_set()
    assert parser.headers_complete_event.is_set()
    assert parser.message_complete_event.is_set()
    assert len(parser.body) == parser.content_length

def test_parse_chunk():
    parser = Response()
    parser.feed(RESPONSE)
    response = StringIO(RESPONSE)
    data = response.read(10)
    while data:
        parser.feed(data)
        data = response.read(10)
    assert parser.message_begin_event.is_set()
    assert parser.headers_complete_event.is_set()
    assert parser.message_complete_event.is_set()
    assert parser.should_keep_alive()
    assert parser.status_code == 301
    assert parser.items() == [
        ('Location', 'http://www.google.fr/'),
        ('Content-Type', 'text/html; charset=UTF-8'),
        ('Date', 'Thu, 13 Oct 2011 15:03:12 GMT'),
        ('Expires', 'Sat, 12 Nov 2011 15:03:12 GMT'),
        ('Cache-Control', 'public, max-age=2592000'),
        ('Server', 'gws'),
        ('Content-Length', '218'),
        ('X-XSS-Protection', '1; mode=block'),
    ]
    assert len(parser.body) == parser.content_length

def test_parse_error():
    response =  Response()
    try:
        response.feed("HTTP/1.1 800\r\n\r\n")
        response.feed("")
        assert response.status_code, 0
        assert response.message_begin_event.is_set()
        assert response.body, None
    except HTTPParseError as e:
        assert str(e) == ""
    else:
        assert False, "should have raised"


def test_client():
    client = Client('google.fr', 80)
    response = client.request("GET", "/")
    assert response.status_code == 301
    print repr(response.body)


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
