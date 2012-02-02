httplib = __import__('httplib')
from geventhttpclient import response
import gevent.socket
import gevent.ssl


class HTTPResponse(response.HTTPSocketResponse):

    def __init__(self, sock, method='GET', strict=0, debuglevel=0,
            buffering=False, **kw):
        if method is None:
            method = 'GET'
        else:
            method = method.upper()
        super(HTTPResponse, self).__init__(sock, method=method, **kw)

    @property
    def version(self):
        v = self.get_http_version()
        if v == 'HTTP/1.1':
            return 11
        return 10

    @property
    def status(self):
        return self.status_code

    @property
    def reason(self):
        return self.msg

    @property
    def msg(self):
        return httplib.responses[self.status_code]

    def _read_status(self):
        return (self.version, self.status_code, self.msg)

    def begin(self):
        pass

    def close(self):
        self.release()

    def isclosed(self):
        return self._sock is None

    def read(self, amt=None):
        return super(HTTPResponse, self).read(amt)

    def getheader(self, name):
        return self[name.lower()]

    def getheaders(self):
        return self._headers_index.items()

    @property
    def will_close(self):
        return self.message_complete and not self.should_keep_alive()

    def _check_close(self):
        return not self.should_keep_alive()


HTTPLibConnection = httplib.HTTPConnection

class HTTPConnection(httplib.HTTPConnection):

    response_class = HTTPResponse

    def __init__(self, *args, **kw):
        HTTPLibConnection.__init__(self, *args, **kw)
        # python 2.6 compat
        if not hasattr(self, "source_address"):
            self.source_address = None

    def connect(self):
        self.sock = gevent.socket.create_connection(
            (self.host,self.port),
            self.timeout, self.source_address)

        if self._tunnel_host:
            self._tunnel()


class HTTPSConnection(HTTPConnection):

    default_port = 443

    def __init__(self, host, port=None, key_file=None, cert_file=None, **kw):
        HTTPConnection.__init__(self, host, port, **kw)
        self.key_file = key_file
        self.cert_file = cert_file

    def connect(self):
        "Connect to a host on a given (SSL) port."

        sock = gevent.socket.create_connection((self.host, self.port),
                                        self.timeout, self.source_address)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = gevent.ssl.wrap_socket(
            sock, self.key_file, self.cert_file)


def patch():
    httplib.HTTPConnection = HTTPConnection
    httplib.HTTPSConnection = HTTPSConnection
    httplib.HTTPResponse = HTTPResponse


