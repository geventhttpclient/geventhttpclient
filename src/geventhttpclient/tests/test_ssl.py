import os
import ssl
from contextlib import contextmanager
from ssl import CertificateError
from unittest import mock

import dpkt.ssl
import gevent.queue
import gevent.server
import gevent.socket
import gevent.ssl
import pytest
from gevent import joinall
from gevent.socket import error as socket_error

from geventhttpclient import HTTPClient
from geventhttpclient.tests.conftest import LISTENER

BASEDIR = os.path.dirname(__file__)
KEY = os.path.join(BASEDIR, "server.key")
CERT = os.path.join(BASEDIR, "server.crt")


@contextmanager
def sslserver(handler, backlog=1):
    exception_queue = gevent.queue.Queue()

    def wrapped_handler(env, start_response):
        try:
            return handler(env, start_response)
        except Exception as e:
            exception_queue.put(e)
            raise

    server = gevent.server.StreamServer(
        LISTENER,
        backlog=backlog,
        handle=wrapped_handler,
        keyfile=KEY,
        certfile=CERT,
        ssl_version=ssl.PROTOCOL_TLS_SERVER,
    )
    server.start()
    try:
        yield server.server_host, server.server_port
        if not exception_queue.empty():
            raise exception_queue.get()
    finally:
        server.stop()


@contextmanager
def timeout_connect_server():
    sock = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_STREAM, 0)
    sock = gevent.ssl.wrap_socket(
        sock, keyfile=KEY, certfile=CERT, ssl_version=ssl.PROTOCOL_TLS_SERVER
    )
    sock.setsockopt(gevent.socket.SOL_SOCKET, gevent.socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", 0))
    sock.listen(1)

    def run(sock):
        conns = []
        while True:
            conn, addr = sock.accept()
            conns.append(conns)
            conn.recv(1024)
            gevent.sleep(10)

    job = gevent.spawn(run, sock)
    try:
        yield sock.getsockname()
        sock.close()
    finally:
        job.kill()


def simple_ssl_response(sock, addr):
    sock.recv(1024)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nConnection: close\r\n\r\n")
    sock.close()


def test_simple_ssl():
    with sslserver(simple_ssl_response) as listener:
        client = HTTPClient(*listener, insecure=True, ssl=True, ssl_options={"ca_certs": CERT})
        response = client.get("/")
        assert response.status_code == 200
        response.read()


def timeout_on_connect(sock, addr):
    sock.recv(1024)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nContent-Length: 0\r\n\r\n")


def test_implicit_sni_from_host_in_ssl():
    server_host, server_port, sent_sni = _get_sni_sent_from_client()
    assert sent_sni == server_host


def test_implicit_sni_from_header_in_ssl():
    server_host, server_port, sent_sni = _get_sni_sent_from_client(
        headers={"host": "ololo_special_host"},
    )
    assert sent_sni == "ololo_special_host"


def test_explicit_sni_in_ssl():
    server_host, server_port, sent_sni = _get_sni_sent_from_client(
        ssl_options={"server_hostname": "test_sni"},
        headers={"host": "ololo_special_host"},
    )
    assert sent_sni == "test_sni"


def _get_sni_sent_from_client(**additional_client_args):
    with sni_checker_server() as ctx:
        server_sock, server_greenlet = ctx
        server_addr, server_port = server_sock.getsockname()[:2]

        mock_addrinfo = (
            gevent.socket.AF_INET,
            gevent.socket.SOCK_STREAM,
            gevent.socket.IPPROTO_TCP,
            "localhost",
            ("127.0.0.1", server_port),
        )
        with mock.patch("gevent.socket.getaddrinfo", mock.Mock(return_value=[mock_addrinfo])):
            server_host = "some_foo"
            client = HTTPClient(
                server_host,
                server_port,
                insecure=True,
                ssl=True,
                connection_timeout=0.1,
                ssl_context_factory=gevent.ssl.create_default_context,
                **additional_client_args,
            )

            def run(http):
                try:
                    http.get("/")
                except socket_error:
                    pass  # handshake will not be completed

            client_greenlet = gevent.spawn(run, client)
            joinall([client_greenlet, server_greenlet])

    return server_host, server_port, server_greenlet.value


@contextmanager
def sni_checker_server():
    sock = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_STREAM, 0)
    sock.setsockopt(gevent.socket.SOL_SOCKET, gevent.socket.SO_REUSEADDR, 1)
    sock.bind(("localhost", 0))
    sock.listen(1)

    # @cyberw 2021-07-10: seems this doesnt exist any more, hope it doesnt make any difference
    # sock.last_seen_sni = None

    def run(sock):
        while True:
            conn, addr = sock.accept()
            client_hello = conn.recv(1024)
            return extract_sni_from_client_hello(client_hello)

    def extract_sni_from_client_hello(hello_packet):
        records, bytes_used = dpkt.ssl.tls_multi_factory(hello_packet)

        for record in records:
            # TLS handshake only
            if record.type != 22:
                continue

            if len(record.data) == 0:
                continue
            # Client Hello only
            if record.data[0] not in (1, chr(1)):
                continue

            handshake = dpkt.ssl.TLSHandshake(record.data)

            ch = handshake.data

            SNI_extension = [
                ext_data
                for (ext_type, ext_data) in ch.extensions
                if ext_type == 0x0  # server_name
            ]
            if SNI_extension:
                SNI_extension = SNI_extension[0]
                sni_list, _ = dpkt.ssl.parse_variable_array(SNI_extension, 2)
                sni_list = sni_list[1:]  # skip SNI entry type
                first_entry, _ = dpkt.ssl.parse_variable_array(sni_list, 2)

                return first_entry.decode()

    job = gevent.spawn(run, sock)
    try:
        yield sock, job
        if job.exception:
            raise job.exception
        sock.close()
    finally:
        job.kill()


def test_timeout_on_connect():
    with timeout_connect_server() as listener:
        client = HTTPClient(*listener, insecure=True, ssl=True, ssl_options={"ca_certs": CERT})

        def run(http, wait_time=100):
            try:
                response = http.get("/")
                gevent.sleep(wait_time)
                response.read()
            except Exception:
                pass

        gevent.spawn(run, client)
        gevent.sleep(0)

        e = None
        try:
            http2 = HTTPClient(
                *listener,
                insecure=True,
                ssl=True,
                connection_timeout=0.1,
                ssl_options={"ca_certs": CERT},
            )
            http2.get("/")
        except gevent.ssl.SSLError as error:
            e = error
        except gevent.socket.timeout as error:
            e = error
        except:
            raise

        assert e is not None, "should have raised"
        if isinstance(e, gevent.ssl.SSLError):
            assert "operation timed out" in str(e)


def network_timeout(sock, addr):
    sock.recv(1024)
    gevent.sleep(10)
    sock.sendall(b"HTTP/1.1 200 Ok\r\nContent-Length: 0\r\n\r\n")


def test_network_timeout():
    with sslserver(network_timeout) as listener:
        client = HTTPClient(
            *listener,
            ssl=True,
            insecure=True,
            network_timeout=0.1,
            ssl_options={"ca_certs": CERT},
        )
        with pytest.raises(gevent.socket.timeout):
            client.get("/")


def check_client_cert_required(client):
    """Make sure hostnames are checked by default."""
    ssl_context = client._connection_pool.ssl_context
    assert ssl_context.check_hostname
    assert ssl_context.verify_mode == gevent.ssl.CERT_REQUIRED
    for socket in client._connection_pool._socket_queue.queue:
        assert socket._context.verify_mode == gevent.ssl.CERT_REQUIRED


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
    assert "ssl.SSLError" in captured
    assert "ALERT_UNKNOWN_CA" in captured


@pytest.mark.network
def test_client_ssl():
    client = HTTPClient("github.com", ssl=True)
    assert client.port == 443
    response = client.get("/")
    assert response.status_code == 200
    body = response.read()
    assert len(body)
    check_client_cert_required(client)


@pytest.mark.network
def test_fail_invalid_ca_certificate():
    certs = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oncert.pem")
    client = HTTPClient("github.com", ssl_options={"ca_certs": certs})
    assert client.port == 443
    with pytest.raises(gevent.ssl.SSLError) as e_info:
        client.get("/")
    assert e_info.value.reason == "CERTIFICATE_VERIFY_FAILED"
    check_client_cert_required(client)
