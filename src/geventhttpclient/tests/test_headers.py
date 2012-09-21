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


def test_create_from_dict():
    header = Headers(dict(ab=1, cd=2, ef=3, gh=4))
    assert len(header) == 4
    assert 'ab' in header

def test_create_from_list():
    header = Headers([('ab', 1), ('cd', 2), ('ef', 3), ('ef', 4), ('ef', 5)])
    assert len(header) == 5
    assert 'ab' in header
    assert len(header['ef']) == 3
    assert header['ef'][0] == 3
    assert header['ef'][-1] == 5

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

