import os
import gevent.queue
import gevent.ssl
import gevent.socket
import certifi
from backports.ssl_match_hostname import match_hostname

try:
    from gevent import lock
except ImportError:
    # gevent < 1.0b2
    from gevent import coros as lock


CA_CERTS = certifi.where()


DEFAULT_CONNECTION_TIMEOUT = 5.0
DEFAULT_NETWORK_TIMEOUT = 5.0


IGNORED = object()


class ConnectionPool(object):

    DEFAULT_CONNECTION_TIMEOUT = 5.0
    DEFAULT_NETWORK_TIMEOUT = 5.0

    def __init__(self, host, port,
            size=5, disable_ipv6=False,
            connection_timeout=DEFAULT_CONNECTION_TIMEOUT,
            network_timeout=DEFAULT_NETWORK_TIMEOUT):
        self._closed = False
        self._host = host
        self._port = port
        self._semaphore = lock.BoundedSemaphore(size)
        self._socket_queue = gevent.queue.LifoQueue(size)

        self.connection_timeout = connection_timeout
        self.network_timeout = network_timeout
        self.size = size
        self.disable_ipv6 = disable_ipv6

    def _resolve(self):
        """ resolve (dns) socket informations needed to connect it.
        """
        family = 0
        if self.disable_ipv6:
            family = gevent.socket.AF_INET
        info = gevent.socket.getaddrinfo(self._host, self._port,
                family, 0, gevent.socket.SOL_TCP)
        # family, socktype, proto, canonname, sockaddr = info[0]
        return info

    def close(self):
        self._closed = True
        while not self._socket_queue.empty():
            try:
                sock = self._socket_queue.get(block=False)
                try:
                    sock.close()
                except:
                    pass
            except gevent.queue.Empty:
                pass

    def _create_tcp_socket(self, family, socktype, protocol):
        """ tcp socket factory.
        """
        sock = gevent.socket.socket(family, socktype, protocol)
        return sock

    def _create_socket(self):
        """ might be overriden and super for wrapping into a ssl socket
            or set tcp/socket options
        """
        sock_infos = self._resolve()
        first_error = None
        for sock_info in sock_infos:
            try:
                sock = self._create_tcp_socket(*sock_info[:3])
            except Exception as e:
                if not first_error:
                    first_error = e
                continue

            try:
                sock.settimeout(self.connection_timeout)
                sock.connect(sock_info[-1])
                self.after_connect(sock)
                sock.settimeout(self.network_timeout)
                return sock
            except IOError as e:
                sock.close()
                if not first_error:
                    first_error = e
            except:
                sock.close()
                raise

        if first_error:
            raise first_error
        else:
            raise RuntimeError("Cannot resolve %s:%s" % (self._host, self._port))


    def after_connect(self, sock):
        pass

    def get_socket(self):
        """ get a socket from the pool. This blocks until one is available.
        """
        self._semaphore.acquire()
        if self._closed:
            raise RuntimeError('connection pool closed')
        try:
            return self._socket_queue.get(block=False)
        except gevent.queue.Empty:
            try:
                return self._create_socket()
            except:
                self._semaphore.release()
                raise

    def return_socket(self, sock):
        """ return a socket to the pool.
        """
        if self._closed:
            try:
                sock.close()
            except:
                pass
            return
        self._socket_queue.put(sock)
        self._semaphore.release()

    def release_socket(self, sock):
        """ call when the socket is no more usable.
        """
        try:
            sock.close()
        except:
            pass
        if not self._closed:
            self._semaphore.release()


class SSLConnectionPool(ConnectionPool):

    default_options = {
        'ssl_version': gevent.ssl.PROTOCOL_SSLv3,
        'ca_certs': CA_CERTS,
        'cert_reqs': gevent.ssl.CERT_REQUIRED
    }

    def __init__(self, host, port, **kw):
        self.ssl_options = self.default_options.copy()
        self.insecure = kw.pop('insecure', False)
        self.ssl_options.update(kw.pop('ssl_options', dict()))
        super(SSLConnectionPool, self).__init__(host, port, **kw)

    def after_connect(self, sock):
        super(SSLConnectionPool, self).after_connect(sock)
        if not self.insecure:
            match_hostname(sock.getpeercert(), self._host)

    def _create_tcp_socket(self, family, socktype, protocol):
        sock = super(SSLConnectionPool, self)._create_tcp_socket(
            family, socktype, protocol)

        return gevent.ssl.wrap_socket(sock, **self.ssl_options)
