import urlparse
from urllib import quote_plus


class URL(object):
    """ A mutable URL class

    You build it from a url string.
    >>> url = URL('http://getgauss.com/urls?param=asdfa')
    >>> url
    URL(http://getgauss.com/urls?param=asdfa)

    You cast it to a tuple, it returns the same tuple as `urlparse.urlsplit`.
    >>> tuple(url)
    ('http', 'getgauss.com', '/urls', 'param=asdfa', '')

    You can cast it as a string.
    >>> str(url)
    'http://getgauss.com/urls?param=asdfa'

    You can manipulate query arguments.
    >>> url.query['auth_token'] = 'asdfaisdfuasdf'
    >>> url
    URL(http://getgauss.com/urls?auth_token=asdfaisdfuasdf&param=asdfa)

    You can change attributes.
    >>> url.host = 'infrae.com'
    >>> url
    URL(http://infrae.com/urls?auth_token=asdfaisdfuasdf&param=asdfa)
    """

    DEFAULT_PORTS = {
        'http': 80,
        'https': 443
    }

    __slots__ = ('scheme', 'host', 'port', 'path', 'query', 'fragment')
    quoting_safe = ''

    def __init__(self, url=None):
        if url is not None:
            self.scheme, netloc, self.path, \
                query, self.fragment = urlparse.urlsplit(url)
        else:
            self.scheme, netloc, self.path, query, self.fragment = \
                'http', '', '/', '', ''
        self.port = None
        self.host = None
        if netloc is not None:
            info = netloc.rsplit(':', 1)
            if len(info) == 2:
                self.host, port = info
                self.port = int(port)
            else:
                self.host = info[0]
                self.port = self.DEFAULT_PORTS.get(self.scheme)
            # for IPv6 hosts
            self.host = self.host.strip('[]')
        if not self.path:
            self.path = "/"
        self.query = {}
        for key, value in urlparse.parse_qs(query).iteritems():
            if len(value) > 1:
                self.query[key] = value
            else:
                self.query[key] = value[0]

    @property
    def netloc(self):
        buf = self.host
        if self.port is None:
            return buf
        elif self.DEFAULT_PORTS.get(self.scheme) == self.port:
            return buf
        buf += ":" + str(self.port)
        return buf

    def __copy__(self):
        return URL(str(self))

    def __repr__(self):
        return "URL(%s)" % str(self)

    def __iter__(self):
        return iter((self.scheme, self.netloc, self.path,
                self.query_string, self.fragment))

    def __str__(self):
        return urlparse.urlunsplit(tuple(self))

    def __eq__(self, other):
        return str(self) == str(other)

    @property
    def query_string(self):
        params = []
        for key, value in self.query.iteritems():
            if isinstance(value, list):
                for item in value:
                    params.append("%s=%s" % (
                        quote_plus(key), quote_plus(str(item), safe=self.quoting_safe)))
            else:
                params.append("%s=%s" % (
                    quote_plus(key), quote_plus(str(value), safe=self.quoting_safe)))
        if params:
            return "&".join(params)
        return ''

    @property
    def request_uri(self):
        query = self.query_string
        if not query:
            return self.path
        return self.path + '?' + query

    def __getitem__(self, key):
        return self.query[key]

    def get(self, key):
        return self.query.get(key)

    def __setitem__(self, key, value):
        self.query[key] = value
        return value

    def append_to_path(self, value):
        if value.startswith('/'):
            if self.path.endswith('/'):
                self.path += value[1:]
                return self.path
        elif not self.path.endswith("/"):
            self.path += "/" + value
            return self.path

        self.path += value
        return self.path

    def redirect(self, other):
        other = type(self)(other)
        if not other.host:
            other.scheme = self.scheme
            other.host = self.host
            other.port = self.port
        if not other.path.startswith('/'):
            if self.path.endswith('/'):
                other.path = self.path + other.path
            else:
                other.path = self.path.rsplit('/', 1)[0] + '/' + other.path
        return other
