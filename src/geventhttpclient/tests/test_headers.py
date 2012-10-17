
from geventhttpclient.client import Header

def test_header_dict_str():
    d = {}
    d[Header("Host")] = "gwikzone.org"

    assert Header('HOST') in d
    assert d[Header('HosT')] == "gwikzone.org"

    assert d['host'] == "gwikzone.org"
    assert 'host' in d 

def test_header_replace():
    d = {}
    d[Header('Content-Type')] = "text/plain"
    d[Header('content-type')] = "text/html"
    assert d['content-type'] == "text/html"
