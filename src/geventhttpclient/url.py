import six
if six.PY3:
    from urllib import parse as urlparse
    from urllib.parse import quote_plus
else:
    import urlparse
    from urllib import quote_plus

DEFAULT_PORTS = {
    'http': 80,
    'https': 443
}


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

    __slots__ = ('scheme', 'host', 'port', 'path', 'query', 'fragment', 'user', 'password')
    quoting_safe = ''

    def __init__(self, url=None):
        if url is not None:
            scheme, netloc, path, query, fragment = urlparse.urlsplit(url)
        else:
            scheme, netloc, path, query, fragment = 'http', '', '/', '', ''

        self.scheme = scheme
        self.fragment = fragment

        user, password, host, port = None, None, '', None
        if netloc:
            if '@' in netloc:
                user_pw, netloc = netloc.rsplit('@', 1)
                if ':' in user_pw:
                    user, password = user_pw.rsplit(':', 1)
                else:
                    user = user_pw

            if netloc.startswith('['):
                host, port_pt = netloc.rsplit(']', 1)
                host = host.strip('[]')
                if port_pt:
                    port = int(port_pt.strip(':'))
            else:
                if ':' in netloc:
                    host, port = netloc.rsplit(':', 1)
                    port = int(port)
                else:
                    host = netloc

        if not port:
            port = DEFAULT_PORTS.get(self.scheme)

        self.host = host
        self.port = port
        self.user = user
        self.password = password

        self.path = path or ''

        self.query = dict()
        for key, value in six.iteritems(urlparse.parse_qs(query)):
            if len(value) > 1:
                self.query[key] = value
            else:
                self.query[key] = value[0]

    @property
    def netloc(self):
        return self.full_netloc(auth=False)

    def full_netloc(self, auth=True):
        buf = ''
        if self.user and auth:
            buf += self.user
            if self.passwort:
                buf += ':' + self.passwort
            buf += '@'

        if ':' in self.host:
            buf += '[' + self.host + ']'
        else:
            buf += self.host
        if self.port is None:
            return buf
        elif DEFAULT_PORTS.get(self.scheme) == self.port:
            return buf
        buf += ':' + str(self.port)
        return buf

    def __copy__(self):
        clone = type(self)()
        for key in self.__slots__:
            val = getattr(self, key)
            if isinstance(val, dict):
                val = val.copy()
            setattr(clone, key, val)
        return clone

    def __repr__(self):
        return "URL(%s)" % str(self)

    def __iter__(self):
        return iter((self.scheme, self.full_netloc(), self.path,
                self.query_string, self.fragment))

    def __str__(self):
        return urlparse.urlunsplit(tuple(self))

    def __eq__(self, other):
        return str(self) == str(other)

    @property
    def query_string(self):
        params = []
        for key, value in six.iteritems(self.query):
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
        """ Redirect to the other URL, relative to the current one """
        if not isinstance(other, type(self)):
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

    def stripped_auth(self):
        """ Remove fragment and authentication for proxy handling """
        clone = type(self)()
        # Copy all fields except fragment, username and password
        for key in self.__slots__[:5]:
            val = getattr(self, key)
            if isinstance(val, dict):
                val = val.copy()
            setattr(clone, key, val)
        return clone
