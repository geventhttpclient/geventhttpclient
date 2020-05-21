import six
from six.moves import xrange
import gevent
import gevent.monkey
gevent.monkey.patch_all()

import pytest

if six.PY2:
    from cookielib import CookieJar
    from urllib2 import Request
else:
    from http.cookiejar import CookieJar
    from urllib.request import Request
import string
import random
import time

from geventhttpclient.response import HTTPResponse
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


def test_create_from_kwargs():
    h = Headers(ab=1, cd=2, ef=3, gh=4)
    assert len(h) == 4
    assert 'ab' in h

def test_create_from_iterator():
    h = Headers((x, x*5) for x in string.ascii_lowercase)
    assert len(h) == len(string.ascii_lowercase)

def test_create_from_dict():
    h = Headers(dict(ab=1, cd=2, ef=3, gh=4))
    assert len(h) == 4
    assert 'ab' in h

def test_create_from_list():
    h = Headers([('ab', 'A'), ('cd', 'B'), ('cookie', 'C'), ('cookie', 'D'), ('cookie', 'E')])
    assert len(h) == 5
    assert 'ab' in h
    assert len(h['cookie']) == 3
    assert h['cookie'][0] == 'C'
    assert h['cookie'][-1] == 'E'

def test_case_insensitivity():
    h = Headers({'Content-Type': 'text/plain'})
    h.add('Content-Encoding', 'utf8')
    for val in ('content-type', 'content-encoding'):
        assert val.upper() in h
        assert val.lower() in h
        assert val.capitalize() in h
        assert h.get(val.lower()) == h.get(val.upper()) == h.get(val.capitalize())
        del h[val.upper()]
        assert val.lower() not in h

def test_read_multiple_header():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    headers = parser._headers_index
    assert len(headers['set-cookie']) == MULTI_COOKIE_RESPONSE.count('Set-Cookie')
    assert headers['set-cookie'][0].startswith('bb_lastvisit')
    assert headers['set-cookie'][-1].startswith('bb_fbprofilepicurl')

@pytest.mark.skip(reason="remote site behavior changed")
def test_cookielib_compatibility():
    cj = CookieJar()
    # Set time in order to be still valid in some years, when cookie strings expire
    cj._now = cj._policy._now = time.mktime((2012, 1, 1, 0, 0, 0, 0, 0, 0))

    request = Request('http://test.com')
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    cookies = cj.make_cookies(parser, request)
    # Don't use extract_cookies directly, as time can not be set there manually for testing
    for cookie in cookies:
        if cj._policy.set_ok(cookie, request):
            cj.set_cookie(cookie)
    # Three valid, not expired cookies placed
    assert len(list(cj)) == 3

def test_compatibility_with_previous_API_read():
    parser = HTTPResponse()
    parser.feed(MULTI_COOKIE_RESPONSE)
    for single_item in ('content-encoding', 'content-type', 'content-length', 'cache-control', 'connection'):
        assert isinstance(parser[single_item], six.string_types)
        assert isinstance(parser.get(single_item), six.string_types)

def test_compatibility_with_previous_API_write():
    h = Headers()
    h['asdf'] = 'jklm'
    h['asdf'] = 'dfdf'
    # Lists only if necessary
    assert h['asdf'] == 'dfdf'

def test_copy():
    rnd_txt = lambda length: ''.join(random.choice(string.ascii_letters) for _ in xrange(length))
    h = Headers((rnd_txt(10), rnd_txt(50)) for _ in xrange(100))
    c = h.copy()
    assert h is not c
    assert len(h) == len(c)
    assert set(h.keys()) == set(c.keys())
    assert h == c
    assert type(h) is type(c)
    for _ in xrange(100):
        rnd_key = rnd_txt(9)
        c[rnd_key] = rnd_txt(10)
        assert rnd_key in c
        assert rnd_key not in h

def test_fieldname_string_enforcement():
    with pytest.raises(Exception):
        Headers({3: 3})
    h = Headers()
    with pytest.raises(Exception):
        h[3] = 5
    with pytest.raises(Exception):
        h.add(3, 4)
    with pytest.raises(Exception):
        del h[3]

def test_header_replace():
    d = Headers()
    d['Content-Type'] = "text/plain"
    d['content-type'] = "text/html"
    assert d['content-type'] == "text/html"

def test_compat_dict():
    h = Headers(D='asdf')
    h.add('E', 'd')
    h.add('E', 'f')
    h.add('Cookie', 'd')
    h.add('Cookie', 'e')
    h.add('Cookie', 'f')
    d = h.compatible_dict()

    for x in ('Cookie', 'D', 'E'):
        assert x in d
    assert d['D'] == 'asdf'
    assert d['E'] == 'd, f'
    assert d['Cookie'] == 'd, e, f'

if __name__ == '__main__':
    test_copy()
    test_compat_dict()
    test_cookielib_compatibility()
