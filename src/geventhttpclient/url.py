from collections.abc import Mapping
from urllib import parse as urlparse

DEFAULT_PORTS = {"http": 80, "https": 443}


class URL:
    """Immutable URL class

    You build it from an url string.
    >>> url = URL('http://python.org/urls?param=asdfa')
    >>> url
    URL(http://python.org/urls?param=asdfa)

    You cast it to a tuple, it returns the same tuple as `urlparse.urlsplit`.
    >>> tuple(url)
    ('http', 'python.org', '/urls', 'param=asdfa', '')

    You can cast it as a string.
    >>> str(url)
    'http://python.org/urls?param=asdfa'
    """

    __slots__ = ("_parsed",)

    def __init__(self, url="", params=None):
        if isinstance(url, str):
            parsed = urlparse.urlparse(url)
        else:
            parsed = url
        scheme, netloc, path, parsed_params, query, fragment = parsed

        if params is not None:
            new_params = _encode_params(params)
            query = query + "&" + new_params if query else new_params
        self._parsed = urlparse.ParseResult(scheme, netloc, path, parsed_params, query, fragment)

    def __str__(self):
        return self._parsed.geturl()

    def __repr__(self):
        return f"URL({self})"

    def __iter__(self):
        return (val if val is not None else "" for val in self._parsed)

    def __eq__(self, other):
        if not isinstance(other, type(self)):
            other = type(self)(other)
        return self._parsed == other._parsed

    def __getattr__(self, attr):
        value = getattr(self._parsed, attr)
        # backwards compatibility: never return None for URL parts
        return value if value is not None else ""

    @property
    def host(self):
        return self.hostname

    @property
    def user(self):
        return self.username

    @property
    def port(self):
        port = self._parsed._hostinfo[1]
        if port is not None:
            if port.isdigit() and port.isascii():
                port = int(port)
            else:
                raise ValueError(f"Port could not be cast to integer value as {port!r}")
            if not (0 <= port <= 65535):
                raise ValueError("Port out of range 0-65535")
        else:
            port = DEFAULT_PORTS.get(self._parsed.scheme)
        return port

    @property
    def query_string(self):
        return self.query

    @property
    def request_uri(self):
        if not self.query:
            return self.path
        return self.path + "?" + self.query

    def redirect(self, other):
        """Redirect to the other URL, relative to the current one."""
        if isinstance(other, str):
            other = URL(other)

        if other.netloc:
            return other

        # relative redirect
        scheme, netloc, path, params, query, fragment = other
        scheme = self.scheme
        netloc = self.netloc
        if not path.startswith("/"):
            if path.endswith("/"):
                path = self.path + path
            else:
                path = self.path.rstrip("/") + "/" + path
        parsed = urlparse.ParseResult(scheme, netloc, path, params, query, fragment)
        return type(self)(parsed)

    @property
    def quoted(self):
        return requote_uri(str(self))

    @property
    def quoted_uri(self):
        return requote_uri(self.request_uri)


def _encode_params(data):
    """Encode parameters in a piece of data.
    Will successfully encode parameters when passed as a dict or a list of 2-tuples.
    """

    if isinstance(data, (str, bytes)):
        return data
    if hasattr(data, "__iter__"):
        result = []
        for k, vs in to_key_val_list(data):
            if isinstance(vs, (str, bytes)) or not hasattr(vs, "__iter__"):
                vs = [vs]
            for v in vs:
                if v is not None:
                    result.append(
                        (
                            k.encode("utf-8") if isinstance(k, str) else k,
                            v.encode("utf-8") if isinstance(v, str) else v,
                        )
                    )
        return urlparse.urlencode(result, doseq=True)
    return data


def to_key_val_list(value):
    """Take an object and test to see if it can be represented as a
    dictionary. If it can be, return a list of tuples, e.g.,
    ::
        >>> to_key_val_list([('key', 'val')])
        [('key', 'val')]
        >>> to_key_val_list({'key': 'val'})
        [('key', 'val')]
        >>> to_key_val_list('string')
        Traceback (most recent call last):
        ...
        ValueError: cannot encode objects that are not 2-tuples
    :rtype: list
    """
    if value is None:
        return None

    if isinstance(value, (str, bytes, bool, int)):
        raise ValueError("cannot encode objects that are not 2-tuples")

    if isinstance(value, Mapping):
        value = value.items()

    return list(value)


class InvalidURL(Exception):
    pass


# The following functions are taken from requests
# Copyright of the original requests project:
# :copyright: (c) 2012 by Kenneth Reitz.
# :license: Apache2, see LICENSE for more details.

# The unreserved URI characters (RFC 3986)
UNRESERVED_SET = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz" + "0123456789-._~"
)


def unquote_unreserved(uri):
    """Un-escape any percent-escape sequences in a URI that are unreserved
    characters. This leaves all reserved, illegal and non-ASCII bytes encoded.

    :rtype: str
    """
    parts = uri.split("%")
    for i in range(1, len(parts)):
        h = parts[i][0:2]
        if len(h) == 2 and h.isalnum():
            try:
                c = chr(int(h, 16))
            except ValueError:
                raise InvalidURL(f"Invalid percent-escape sequence: '{h}'")

            if c in UNRESERVED_SET:
                parts[i] = c + parts[i][2:]
            else:
                parts[i] = f"%{parts[i]}"
        else:
            parts[i] = f"%{parts[i]}"
    return "".join(parts)


def requote_uri(uri):
    """Re-quote the given URI.

    This function passes the given URI through an unquote/quote cycle to
    ensure that it is fully and consistently quoted.

    :rtype: str
    """
    safe_with_percent = "!#$%&'()*+,/:;=?@[]~"
    safe_without_percent = "!#$&'()*+,/:;=?@[]~"
    try:
        # Unquote only the unreserved characters
        # Then quote only illegal characters (do not quote reserved,
        # unreserved, or '%')
        return urlparse.quote(unquote_unreserved(uri), safe=safe_with_percent)
    except InvalidURL:
        # We couldn't unquote the given URI, so let's try quoting it, but
        # there may be unquoted '%'s in the URI. We need to make sure they're
        # properly quoted so they do not cause issues elsewhere.
        return urlparse.quote(uri, safe=safe_without_percent)
