import os
import gevent.queue
import gevent.coros
import gevent.ssl
import gevent.socket


CA_CERTS = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "cacert.pem")


class ConnectionPool(object):

    DEFAULT_CONNECTION_TIMEOUT = 5
    DEFAULT_NETWORK_TIMEOUT = 5

    def __init__(self, host, port,
            size=5, disable_ipv6=False,
            connection_timeout=DEFAULT_CONNECTION_TIMEOUT,
            network_timeout=DEFAULT_NETWORK_TIMEOUT):
        self._host = host
        self._port = port
        self.size = size
        self._semaphore = gevent.coros.BoundedSemaphore(size)
        self._socket_queue = gevent.queue.LifoQueue(size)
        self._connection_timeout = connection_timeout
        self.disable_ipv6 = disable_ipv6
        self._resolve()

    def _resolve(self):
        """ resolve (dns) socket informations needed to connect it.
        """
        family = 0
        if self.disable_ipv6:
            family = gevent.socket.AF_INET

        info = gevent.socket.getaddrinfo(self._host, self._port,
                family, 0, gevent.socket.SOL_TCP)

        family, socktype, proto, canonname, sockaddr = info[0]

        self._sock_family = family
        self._sock_type = socktype
        self._sock_protocol  = proto
        self._sock_address = sockaddr

    def _create_tcp_socket(self):
        """ tcp socket factory.
        """
        sock = gevent.socket.socket(self._sock_family,
            self._sock_type,
            self._sock_protocol)
        return sock

    def _create_socket(self):
        """ might be overriden and super for wrapping into a ssl socket
            or set tcp/socket options
        """
        return self._create_tcp_socket()

    def _connect_socket(self, sock):
        """ Connect a socket.
        """
        with gevent.Timeout(self._connection_timeout):
            sock.connect(self._sock_address)

    def get_socket(self):
        """ get a socket from the pool. This blocks until one is available.
        """
        self._semaphore.acquire()
        try:
            return self._socket_queue.get(block=False)
        except gevent.queue.Empty:
            sock = self._create_socket()
            self._connect_socket(sock)
            return sock

    def return_socket(self, sock):
        """ return a socket to the pool.
        """
        self._socket_queue.put(sock)
        self._semaphore.release()

    def release_socket(self, sock):
        """ call when the socket is no more usable.
        """
        try:
            sock.close()
        except:
            pass
        self._semaphore.release()


class SSLConnectionPool(ConnectionPool):

    default_options = {
        'ca_certs': CA_CERTS
    }

    def __init__(self, host, port, **kw):
        self.ssl_options = self.default_options.copy()
        self.ssl_options.update(kw['ssl_options'])
        del kw['ssl_options']
        super(SSLConnectionPool, self).__init__(host, port, **kw)

    def _create_socket(self):
        sock = super(SSLConnectionPool, self)._create_socket()
        ssl_sock = gevent.ssl.wrap_socket(sock, **self.ssl_options)
        return ssl_sock


