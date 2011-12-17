import os
import pytest
from geventhttpclient import HTTPClient
from gevent.ssl import SSLError
import gevent.pool


def test_client_simple():
    client = HTTPClient('www.google.fr')
    assert client.port == 80
    response = client.get('/')
    assert response.status_code == 200
    body = response.read()
    assert len(body)

def test_client_ssl():
    client = HTTPClient('www.google.fr', ssl=True)
    assert client.port == 443
    response = client.get('/')
    assert response.status_code == 200
    body = response.read()
    assert len(body)

def test_ssl_fail_invalid_certificate():
    certs = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "onecert.pem")
    client = HTTPClient('www.google.fr', ssl_options={'ca_certs': certs})
    assert client.port == 443
    with pytest.raises(SSLError):
        client.get('/')

def test_multi_queries_greenlet_safe():
    client = HTTPClient('www.google.fr', concurrency=3)
    group = gevent.pool.Group()
    def run(i):
        response = client.get('/')
        return response, response.read()

    count = 0
    for response, content in group.imap_unordered(run, xrange(5)):
        assert response.status_code == 200
        assert len(content)
        count += 1
    assert count == 5


