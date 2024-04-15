import pytest

from geventhttpclient.header import Headers
from geventhttpclient.requests import Session
from geventhttpclient.tests.conftest import HTTPBIN_HOST


@pytest.mark.network
def test_no_form_encode_header():
    url = f"https://{HTTPBIN_HOST}/headers"
    hdrs = Headers(Session().get(url).json()["headers"])
    print(hdrs)
    assert "content-type" not in hdrs
    assert "content-length" not in hdrs
