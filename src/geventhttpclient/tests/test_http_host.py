import pytest

from geventhttpclient import HTTPClient
from geventhttpclient.client import METHOD_GET


@pytest.mark.parametrize("port", [None, 1234])
@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "[::1]"])
def test_build_request_host(host, port):
    http = HTTPClient(host, port)
    host_ref = (
        f"host: {f'[{host}]' if host.startswith(':') else host}{f':{port}' if port else ''}\r\n"
    )
    assert host_ref in http._build_request(METHOD_GET, "").lower()
