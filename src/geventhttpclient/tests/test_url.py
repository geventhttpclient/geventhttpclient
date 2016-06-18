import six
from geventhttpclient.url import URL

url_full = 'http://getgauss.com/subdir/file.py?param=value&other=true#frag'
url_path_only = '/path/to/something?param=value&other=true'

def test_simple_url():
    url = URL(url_full)
    assert url.path == '/subdir/file.py'
    assert url.host == 'getgauss.com'
    assert url.port == 80
    assert url['param'] == 'value'
    assert url['other'] == 'true'
    assert url.fragment == 'frag'

def test_path_only():
    url = URL(url_path_only)
    assert url.host == ''
    assert url.port == None
    assert url.path == '/path/to/something'
    assert url['param'] == 'value'
    assert url['other'] == 'true'

def test_empty():
    url = URL()
    assert url.host == ''
    assert url.port == 80
    assert url.query == {}
    assert url.fragment == ''
    assert url.netloc == ''
    assert str(url) == 'http:///'

def test_empty_path():
    assert URL('http://getgauss.com').path == ''

def test_consistent_reparsing():
    for surl in (url_full, url_path_only):
        url = URL(surl)
        reparsed = URL(str(url))
        for attr in URL.__slots__:
            assert getattr(reparsed, attr) == getattr(url, attr)

def test_redirection_abs_path():
    url = URL(url_full)
    updated = url.redirect('/test.html')
    assert updated.host == url.host
    assert updated.port == url.port
    assert updated.path == '/test.html'
    assert updated.query == {}
    assert updated.fragment == ''

def test_redirection_rel_path():
    url = URL(url_full)
    for redir in ('test.html?key=val', 'folder/test.html?key=val'):
        updated = url.redirect(redir)
        assert updated.host == url.host
        assert updated.port == url.port
        assert updated.path.startswith('/subdir/')
        assert updated.path.endswith(redir.split('?', 1)[0])
        assert updated.query == {'key': 'val'}
        assert updated.fragment == ''

def test_redirection_full_path():
    url_full2_plain = 'http://google.de/index'
    url = URL(url_full)
    updated = url.redirect(url_full2_plain)
    url_full2 = URL(url_full2_plain)
    for attr in URL.__slots__:
        assert getattr(updated, attr) == getattr(url_full2, attr)
    assert str(url_full2) == url_full2_plain

def test_set_safe_encoding():
    class SafeModURL(URL):
        quoting_safe = '*'
    surl = '/path/to/something?param=value&other=*'

    assert URL(surl).query_string == 'other=%2A&param=value' or  URL(surl).query_string == 'param=value&other=%2A'
    assert SafeModURL(surl).query_string == 'other=*&param=value' or SafeModURL(surl).query_string == 'param=value&other=*'
    URL.quoting_safe = '*'
    assert URL(surl).query_string == 'other=*&param=value' or URL(surl).query_string == 'param=value&other=*'
    URL.quoting_safe = ''

def test_equality():
    assert URL('https://example.com/') != URL('http://example.com/')
    assert URL('http://example.com/') == URL('http://example.com/')

def test_pw():
    url = URL('http://asdf:dd@heise.de/index.php?aaaa=bbbbb')
    assert url.host == 'heise.de'
    assert url.port == 80
    assert url.user == 'asdf'
    assert url.password == 'dd'

def test_pw_with_port():
    url = URL('http://asdf:dd@heise.de:90/index.php?aaaa=bbbbb')
    assert url.host == 'heise.de'
    assert url.port == 90
    assert url.user == 'asdf'
    assert url.password == 'dd'

def test_ipv6():
    url = URL('http://[2001:db8:85a3:8d3:1319:8a2e:370:7348]/')
    assert url.host == '2001:db8:85a3:8d3:1319:8a2e:370:7348'
    assert url.port == 80
    assert url.user == None

def test_ipv6_with_port():
    url = URL('https://[2001:db8:85a3:8d3:1319:8a2e:370:7348]:8080/')
    assert url.host == '2001:db8:85a3:8d3:1319:8a2e:370:7348'
    assert url.port == 8080
    assert url.user == None

if __name__ == '__main__':
    test_redirection_abs_path()
    test_redirection_rel_path()
    test_redirection_full_path()
    test_ipv6_with_port()
