import sys
from ssl import CertificateError

import gevent
import pytest

from geventhttpclient import HTTPClient
from tests.test_ssl import CERT, check_client_cert_required, simple_ssl_response, sslserver

# Local SSL tests relying on the gevent SSL server. The client-side handshake
# on a stdlib SSLSocket (non-gevent mode) blocks the OS thread, so the
# gevent-based test server cannot make progress; these tests only work
# monkey patched.


def test_simple_ssl():
    with sslserver(simple_ssl_response) as listener:
        client = HTTPClient(*listener, insecure=True, ssl=True, ssl_options={"ca_certs": CERT})
        response = client.get("/")
        assert response.status_code == 200
        response.read()


def test_verify_self_signed_fail(capsys):
    with sslserver(simple_ssl_response) as listener:
        client = HTTPClient(*listener, ssl=True)
        with pytest.raises(CertificateError) as raised:
            client.get("/")
        assert "CERTIFICATE_VERIFY_FAILED" in str(raised.value)
        assert raised.value.verify_message == "self-signed certificate"
        check_client_cert_required(client)
        client.close()

    # This tests breaking server side socket confusingly prints its certificate error message delayed
    # into other tests output, if we don't give it a split second for printing now.
    gevent.sleep(0.01)
    captured = capsys.readouterr().err
    if sys.platform == "win32":
        # Windows tears the connection down before the TLS alert reaches the server.
        assert "ConnectionResetError" in captured or "ssl.SSLError" in captured
    else:
        assert "ssl.SSLError" in captured
        assert "ALERT_UNKNOWN_CA" in captured
