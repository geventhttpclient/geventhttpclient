import http.client
import os
import ssl
from contextlib import contextmanager
from unittest import mock
from unittest.mock import MagicMock, patch

import dpkt.ssl
import gevent.queue
import gevent.server
import gevent.socket
import gevent.ssl
import pytest
from gevent import joinall
from gevent.socket import error as socket_error

from geventhttpclient import HTTPClient, httplib
from geventhttpclient.connectionpool import SSLConnectionPool
from tests.common import LISTENER

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
        gevent.sleep(0.001)


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

    # @cyberw 2021-07-10: seems this doesn't exist any more, hope it doesn't make any difference
    # sock.last_seen_sni = None

    def run(sock):
        while True:
            conn, addr = sock.accept()
            client_hello = conn.recv(4096)
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


@patch("ssl.create_default_context")
def test_ssl_context_cert_and_keyfile(mock_create_default_context):
    mock_ssl_context = MagicMock()
    mock_create_default_context.return_value = mock_ssl_context

    ssl_options = {
        "certfile": "/path/to/certfile.pem",
        "keyfile": "/path/to/keyfile.pem",
        "ca_certs": "/path/to/ca-certificates.crt",
    }
    http_client = HTTPClient(
        "github.com", ssl_context_factory=ssl.create_default_context, ssl_options=ssl_options
    )

    mock_create_default_context.assert_called_once_with(cafile=ssl_options["ca_certs"])
    mock_ssl_context.load_cert_chain.assert_called_once_with(
        certfile=ssl_options["certfile"], keyfile=ssl_options["keyfile"]
    )
    assert isinstance(http_client, HTTPClient)


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


def _pool_context(**ssl_options):
    """The TLS context a pool would use for a connection."""
    pool = SSLConnectionPool("localhost", 443, "localhost", 443, ssl_options=ssl_options or None)
    return pool.ssl_context


def test_the_cipher_list_is_left_to_openssl():
    """We ship no cipher list, so OpenSSL decides what is acceptable."""
    reference = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    assert [c["name"] for c in _pool_context().get_ciphers()] == [
        c["name"] for c in reference.get_ciphers()
    ]
    assert "ciphers" not in SSLConnectionPool.default_options


def test_requested_ciphers_are_used():
    """An explicit cipher list still reaches the context."""
    names = [c["name"] for c in _pool_context(ciphers="AES256-GCM-SHA384").get_ciphers()]
    assert "AES256-GCM-SHA384" in names
    assert "AES128-SHA" not in names


# --- the connection classes as a drop-in replacement for http.client ------


def test_https_connection_checks_the_hostname_like_http_client():
    """A connection of ours must not be quieter about the hostname than http.client."""
    reference = http.client.HTTPSConnection("example.com")
    ours = httplib.HTTPSConnection("example.com")
    assert ours._context.check_hostname is True
    assert ours._context.check_hostname == reference._context.check_hostname
    assert ours._context.verify_mode == reference._context.verify_mode
    assert ours._context.post_handshake_auth == reference._context.post_handshake_auth


def test_the_deprecated_arguments_keep_their_positional_places():
    """3.10 and 3.11 took key_file, cert_file, timeout, source_address by position."""
    conn = httplib.HTTPSConnection("example.com", 8443, None, None, 5.0, ("", 0))
    assert conn.port == 8443
    assert conn.timeout == 5.0
    assert conn.source_address == ("", 0)


def test_cert_file_and_key_file_hand_a_client_certificate_to_the_context():
    """What 3.10 and 3.11 did with them, we do: load_cert_chain, same errors."""
    with pytest.raises(gevent.ssl.SSLError, match="PEM lib"):
        httplib.HTTPSConnection("example.com", cert_file=CERT, key_file=CERT)
    with pytest.raises(FileNotFoundError):
        httplib.HTTPSConnection(
            "example.com", cert_file=CERT, key_file=os.path.join(BASEDIR, "missing.key")
        )
    # a key without a certificate: the same TypeError from the same OpenSSL call
    with pytest.raises(TypeError, match="certfile should be a valid filesystem path"):
        httplib.HTTPSConnection("example.com", key_file=KEY)


def test_check_hostname_needs_a_verifying_context():
    """The stdlib refuses a hostname check on an unverified context; so do we."""
    context = gevent.ssl.SSLContext(gevent.ssl.PROTOCOL_TLS_CLIENT)
    # check_hostname has to go first, OpenSSL refuses the combination otherwise
    context.check_hostname = False
    context.verify_mode = gevent.ssl.CERT_NONE
    with pytest.raises(ValueError, match="check_hostname needs a SSL context"):
        httplib.HTTPSConnection("example.com", context=context, check_hostname=True)


def test_check_hostname_defaults_to_what_the_context_says():
    """An unset check_hostname is inherited, not forced on or off."""
    context = gevent.ssl.create_default_context()
    context.check_hostname = False
    conn = httplib.HTTPSConnection("example.com", context=context)
    assert conn._context is context
    assert conn._context.check_hostname is False


def test_the_deprecated_arguments_warn_like_the_stdlib():
    with pytest.warns(DeprecationWarning, match="key_file, cert_file and check_hostname"):
        httplib.HTTPSConnection("example.com", check_hostname=True)


def test_key_file_and_cert_file_attributes_mirror_the_arguments():
    """3.10 and 3.11 stored both, unset ones as None; readers rely on that."""
    conn = httplib.HTTPSConnection("example.com")
    assert conn.key_file is None
    assert conn.cert_file is None


@contextmanager
def mtls_server():
    """A TLS server that talks to nobody who brings no certificate of its own."""
    listener = gevent.socket.socket(gevent.socket.AF_INET, gevent.socket.SOCK_STREAM)
    listener.setsockopt(gevent.socket.SOL_SOCKET, gevent.socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    seen: gevent.queue.Queue = gevent.queue.Queue()

    def run() -> None:
        plain, _ = listener.accept()
        try:
            # build the context explicitly and force TLS 1.2: on Windows the
            # wrap_socket shortcut with cert_reqs=CERT_REQUIRED finishes the
            # TLS 1.3 handshake before the client cert ever gets asked for,
            # so the connection dies with ConnectionAbortedError instead of
            # completing; on 1.2 the cert is exchanged during the handshake
            # itself, which the same code path on every platform agrees on
            ctx = gevent.ssl.SSLContext(gevent.ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=CERT, keyfile=KEY)
            ctx.load_verify_locations(cafile=CERT)
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.maximum_version = gevent.ssl.TLSVersion.TLSv1_2
            ctx.minimum_version = gevent.ssl.TLSVersion.TLSv1_2
            conn = gevent.ssl.SSLSocket(plain, server_side=True, _context=ctx)
            seen.put(conn.recv(1024))
            conn.close()
        except Exception as exc:
            seen.put(exc)
        finally:
            plain.close()

    job = gevent.spawn(run)
    try:
        yield listener.getsockname(), seen
    finally:
        job.kill()
        listener.close()
        gevent.sleep(0.001)


def _client_context() -> gevent.ssl.SSLContext:
    """Trust our own test certificate, and do not mind that it names another host."""
    context = gevent.ssl.SSLContext(gevent.ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=CERT)
    context.check_hostname = False
    # keep the mTLS server and its client on the same TLS 1.2 line so the
    # handshake cannot pick the 1.3 post-handshake auth path that Windows
    # gevent.ssl does not negotiate reliably here
    context.maximum_version = gevent.ssl.TLSVersion.TLSv1_2
    context.minimum_version = gevent.ssl.TLSVersion.TLSv1_2
    return context


def test_client_certificate_reaches_the_server():
    """cert_file and key_file have to buy what they promise: a mutual handshake."""
    with mtls_server() as ((host, port), seen):
        conn = httplib.HTTPSConnection(
            host, port, cert_file=CERT, key_file=KEY, context=_client_context()
        )
        conn.request("GET", "/")
        conn.close()
        assert b"GET / " in seen.get(timeout=10)


def test_server_without_client_certificate_is_refused():
    """The same connection without them gets no further than the handshake."""
    with mtls_server() as ((host, port), seen):
        conn = httplib.HTTPSConnection(host, port, context=_client_context())
        # the handshake may be refused on either side, the platform decides
        # which one notices first, and either side raising is enough
        try:
            conn.request("GET", "/")
        except (gevent.ssl.SSLError, OSError):
            pass
        conn.close()
        outcome = seen.get(timeout=10)
        assert isinstance(outcome, gevent.ssl.SSLError), outcome
