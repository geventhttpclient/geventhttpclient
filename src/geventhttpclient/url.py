from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from typing import Any, overload
from urllib import parse as urlparse

DEFAULT_PORTS = {"http": 80, "https": 443}

# types accepted for query string parameters
ParamsDataType = Mapping[str, Any] | Iterable[tuple[str, Any]] | str | bytes


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

    def __init__(self, url: str | urlparse.ParseResult = "", params: ParamsDataType | None = None):
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

    def __getattr__(self, attr: str) -> Any:
        value = getattr(self._parsed, attr)
        # backwards compatibility: never return None for URL parts
        return value if value is not None else ""

    @property
    def host(self) -> str:
        return self.hostname

    @property
    def user(self) -> str:
        return self.username

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

    def redirect(self, other: str | URL) -> URL:
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
