from cookielib import CookieJar
from urllib2 import Request
import time

from geventhttpclient.response import HTTPResponse
from geventhttpclient._parser import HTTPParseError
from geventhttpclient.header import Headers


MULTI_COOKIE_RESPONSE = """
HTTP/1.1 200 OK
Server: nginx
Date: Fri, 21 Sep 2012 18:49:35 GMT
Content-Type: text/html; charset=windows-1251
Connection: keep-alive
X-Powered-By: PHP/5.2.17
Set-Cookie: bb_lastvisit=1348253375; expires=Sat, 21-Sep-2013 18:49:35 GMT; path=/
Set-Cookie: bb_lastactivity=0; expires=Sat, 21-Sep-2013 18:49:35 GMT; path=/
Cache-Control: private
Pragma: private
Set-Cookie: bb_sessionhash=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_referrerid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_userid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_password=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_lastvisit=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_lastactivity=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_threadedmode=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_userstyleid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_languageid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_fbaccesstoken=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_fbprofilepicurl=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=/
Set-Cookie: bb_sessionhash=abcabcabcabcabcabcabcabcabcabcab; path=/; HttpOnly
Set-Cookie: tapatalk_redirect3=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_sessionhash=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utma=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utmb=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utmc=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: __utmz=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: vbulletin_collapse=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_referrerid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_userid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_password=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_lastvisit=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_lastactivity=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_threadedmode=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_userstyleid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_languageid=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_fbaccesstoken=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Set-Cookie: bb_fbprofilepicurl=deleted; expires=Thu, 22-Sep-2011 18:49:34 GMT; path=1; domain=forum.somewhere.com
Content-Encoding: gzip
Content-Length: 26186

""".lstrip().replace('\n', '\r\n')
# Do not remove the final empty line!


def test_create_from_dict():
    h = Headers(dict(ab=1, cd=2, ef=3, gh=4))
    assert len(h) == 4
    assert 'ab' in h

def test_create_from_list():
    h = Headers([('ab', 'A'), ('cd', 'B'), ('ef', 'C'), ('ef', 'D'), ('ef', 'E')])
    assert len(h) == 5
    assert 'ab' in h
    assert len(h['ef']) == 3
    assert h['ef'][0] == 'C'
    assert h['ef'][-1] == 'E'

def test_case_insensitivity():
    h = Headers({'Content-Type': 'text/plain'})
    assert 'content-type' in h
    assert h.get('content-type') == h.get('CONTENT-TYPE') == h.get('Content-Type') == ['text/plain']

def test_automatic_string_conversion():
    h = Headers()
    h['asdf'] = [5, 6, 7]
    assert h['asdf'] == ['5', '6', '7']
    
def test_read_multiple_header():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    headers = parser._headers_index
    assert len(headers['set-cookie']) == MULTI_COOKIE_RESPONSE.count('Set-Cookie')
    assert headers['set-cookie'][0].startswith('bb_lastvisit')
    assert headers['set-cookie'][-1].startswith('bb_fbprofilepicurl')

def test_cookielib_compatibility():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    cj = CookieJar()
    # Set time in order to be still valid in some years, when cookie strings expire
    cj._now = time.mktime((2012, 1, 1, 0, 0, 0, 0, 0, 0))
    cj.extract_cookies(parser, Request(''))
    # Three valid, not expired cookies placed
    assert len(list(cj)) == 3

def test_compatibility_with_previous_API_read():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    for single_item in ('content-encoding', 'content-type', 'content-length', 'cache-control', 'connection'):
        assert isinstance(parser[single_item], basestring)
        assert isinstance(parser.get(single_item), basestring)

def test_compatibility_with_previous_API_write():
    h = Headers()
    h['asdf'] = 'jklm'
    h['asdf'] = 'dfdf'
    # Only one header should be stored internally
    assert h['asdf'] == ['dfdf']
    
