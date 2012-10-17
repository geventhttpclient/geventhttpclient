from geventhttpclient.url import URL

def test_simple_url():
    surl = 'http://getgauss.com/?param=value&other=true'
    url = URL(surl)
    assert url.path == '/'
    assert url.host == 'getgauss.com'
    assert url.port == 80
    assert url['param'] == 'value'
    assert url['other'] == 'true'

def test_path_only():
    surl = '/path/to/something?param=value&other=true'
    url = URL(surl)
    assert url.host == ''
    assert url.port == None
    assert url.path == '/path/to/something'
    assert url['param'] == 'value'
    assert url['other'] == 'true'
    reparsed = URL(str(url))
    assert reparsed.query == url.query
    assert reparsed.path == url.path
    assert reparsed.host == ''
    assert reparsed.port == None

def test_empty():
    url = URL()
    assert url.host == ''
    assert url.port == 80
    assert url.query == {}
    assert url.fragment == ''
    assert url.netloc == ''
    str(url) == 'http:///'

def test_no_quote():
    surl = '/path/to/something?param=value&other=tr*ue'
    url1 = URL(surl, quoter=str)
    url2 = URL(surl)
    assert url1.query_string == "other=tr*ue&param=value"
    assert url2.query_string == "other=tr%2Aue&param=value"
