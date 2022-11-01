import pytest
from geventhttpclient import HTTPClient
from geventhttpclient.client import METHOD_GET

def test_domain():
    http = HTTPClient('localhost')
    assert http._build_request(METHOD_GET, "").lower().find("host: localhost\r\n") > 0

    http = HTTPClient('localhost', 1234)
    assert http._build_request(METHOD_GET, "").lower().find("host: localhost:1234\r\n") > 0

def test_ipv4():
    http = HTTPClient('127.0.0.1')
    assert http._build_request(METHOD_GET, "").lower().find("host: 127.0.0.1\r\n") > 0

    http = HTTPClient('127.0.0.1', 1234)
    assert http._build_request(METHOD_GET, "").lower().find("host: 127.0.0.1:1234\r\n") > 0

def test_ipv6():
    http = HTTPClient('[::1]')
    assert http._build_request(METHOD_GET, "").lower().find("host: [::1]\r\n") > 0

    http = HTTPClient('[::1]', 1234)
    assert http._build_request(METHOD_GET, "").lower().find("host: [::1]:1234\r\n") > 0

    http = HTTPClient('::1')
    assert http._build_request(METHOD_GET, "").lower().find("host: [::1]\r\n") > 0

    http = HTTPClient('::1', 1234)
    assert http._build_request(METHOD_GET, "").lower().find("host: [::1]:1234\r\n") > 0
