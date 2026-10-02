from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, overload
from urllib import parse as urlparse

DEFAULT_PORTS = {"http": 80, "https": 443}

# types accepted for query string parameters
ParamsDataType = Mapping[str, Any] | Iterable[tuple[str, Any]] | str | bytes


def _or_empty(value: str | None) -> str:
    """Backwards compatibility: never return None for URL parts."""
    return value if value is not None else ""


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

    def __init__(
        self, url: str | urlparse.ParseResult = "", params: ParamsDataType | None = None
    ) -> None:
        if isinstance(url, str):
            parsed = urlparse.urlparse(url)
        else:
            parsed = url
        scheme, netloc, path, parsed_params, query, fragment = parsed

        if params is not None:
            new_params = _encode_params(params)
            # bytes params are not supported together with a str query
            query = query + "&" + new_params if query else new_params  # type: ignore[operator,assignment]
        self._parsed = urlparse.ParseResult(scheme, netloc, path, parsed_params, query, fragment)

    def __str__(self) -> str:
        return self._parsed.geturl()

    def __repr__(self) -> str:
        return f"URL({self})"

    def __iter__(self) -> Iterator[str]:
        return (val if val is not None else "" for val in self._parsed)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            other = type(self)(other)  # type: ignore[arg-type]
        return self._parsed == other._parsed

    # Each part of the URL is exposed as its own property. A single
    # __getattr__ delegate used to forward everything to the parse result,
    # which turned every part into Any for type checkers and silently passed
    # unknown attribute names through.

    @property
    def scheme(self) -> str:
        return self._parsed.scheme

    @property
    def netloc(self) -> str:
        return self._parsed.netloc

    @property
    def path(self) -> str:
        return self._parsed.path

    @property
    def params(self) -> str:
        return self._parsed.params

    @property
    def query(self) -> str:
        return self._parsed.query

    @property
    def fragment(self) -> str:
        return self._parsed.fragment

    @property
    def username(self) -> str:
        return _or_empty(self._parsed.username)

    @property
    def user(self) -> str:
        return self.username

    @property
    def password(self) -> str:
        return _or_empty(self._parsed.password)

    @property
    def hostname(self) -> str:
        return _or_empty(self._parsed.hostname)

    @property
    def host(self) -> str:
        return self.hostname

    @property
    def port(self) -> int | None:
        port = self._parsed.port
        if port is None:
            return DEFAULT_PORTS.get(self._parsed.scheme)
        return port

    @property
    def query_string(self) -> str:
        return self.query

    @property
    def request_uri(self) -> str:
        if not self.query:
            return self.path
        return self.path + "?" + self.query

    def geturl(self) -> str:
        """Alias of str(url), mirroring the parse result's own name for it."""
        return self._parsed.geturl()

    @staticmethod
    def _remove_dot_segments(path: str) -> str:
        """RFC 3986 section 5.2.4: resolve "." and ".." segments."""
        segments: list[str] = []
        while path:
            if path.startswith("../"):
                path = path[3:]
            elif path.startswith("./"):
                path = path[2:]
            elif path.startswith("/./"):
                path = "/" + path[3:]
            elif path == "/.":
                path = "/"
            elif path.startswith("/../"):
                path = "/" + path[4:]
                if segments:
                    segments.pop()
            elif path == "/..":
                path = "/"
                if segments:
                    segments.pop()
            elif path in (".", ".."):
                path = ""
            else:
                cut = path.find("/", 1) if path.startswith("/") else path.find("/")
                if cut == -1:
                    segments.append(path)
                    path = ""
                else:
                    segments.append(path[:cut])
                    path = path[cut:]
        return "".join(segments)

    def redirect(self, other: str | URL) -> URL:
        """Redirect to the other URL, relative to the current one.

        The reference is resolved per RFC 3986 section 5.2: dot segments are
        removed from the resolved path (section 5.2.4) and a relative path is
        merged against the base path (section 5.3).
        """
        if isinstance(other, str):
            other = URL(other)

        if other.scheme:
            # RFC 3986 section 5.2.2: a reference with a scheme replaces
            # scheme, authority and path entirely.
            resolved = other
        elif other.netloc:
            # protocol-relative reference ("//host/path"): section 5.2.2
            # resolves it against the base URI, which keeps the base
            # scheme. Returning `other` unchanged left the scheme empty and
            # HTTPClient.from_url then opened a plain-HTTP connection - an
            # https-to-http downgrade an attacker can force with a single
            # Location header on a TLS connection.
            resolved = type(self)(
                urlparse.ParseResult(
                    self.scheme,
                    other.netloc,
                    other.path,
                    other.params,
                    other.query,
                    other.fragment,
                )
            )
        else:
            # relative reference
            scheme, netloc, path, params, query, fragment = other
            scheme = self.scheme
            netloc = self.netloc
            if not path and not params:
                # RFC 3986 section 5.2.2: a reference with an empty path keeps
                # the base path as-is and, unless it defines a query of its
                # own, the base query. Running "?x=1" through the merge below
                # turned /a/b into /a/b/?x=1, which path-sensitive servers and
                # caches treat as a different resource. The path parameters
                # (";p", split into their own field by urlparse) belong to the
                # base path and are kept with it.
                path = self.path
                params = self.params
                if not query:
                    query = self.query
            elif not path.startswith("/"):
                # RFC 3986 section 5.3: merge against all but the last
                # segment of the base path. Appending to the full base path
                # instead turned /dir/page + test.html into
                # /dir/page/test.html, a resource that does not exist.
                if not self.path:
                    path = "/" + path
                elif self.path.endswith("/"):
                    path = self.path + path
                else:
                    path = self.path[: self.path.rfind("/") + 1] + path
            resolved = type(self)(
                urlparse.ParseResult(scheme, netloc, path, params, query, fragment)
            )
        return type(self)(
            urlparse.ParseResult(
                resolved.scheme,
                resolved.netloc,
                self._remove_dot_segments(resolved.path),
                resolved.params,
                resolved.query,
                resolved.fragment,
            )
        )

    @property
    def quoted(self) -> str:
        return requote_uri(str(self))

    @property
    def quoted_uri(self) -> str:
        return requote_uri(self.request_uri)


def _encode_params(data: ParamsDataType | None) -> str | bytes:
    """Encode parameters in a piece of data.
    Will successfully encode parameters when passed as a dict or a list of 2-tuples.
    """

    if isinstance(data, (str, bytes)):
        return data
    if data is None:
        return data  # type: ignore[return-value]
    if not hasattr(data, "__iter__"):
        return data  # type: ignore[return-value]
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


@overload
def to_key_val_list(value: None) -> None: ...


@overload
def to_key_val_list(value: ParamsDataType) -> list[tuple[Any, Any]]: ...


def to_key_val_list(value: ParamsDataType | None) -> list[tuple[Any, Any]] | None:
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
        raise ValueError("cannot encode objects that are not 2-tuples")  # noqa: TRY004

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


def unquote_unreserved(uri: str) -> str:
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


def requote_uri(uri: str) -> str:
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
