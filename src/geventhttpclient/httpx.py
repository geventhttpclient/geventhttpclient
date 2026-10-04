"""An httpx-compatible interface on top of the geventhttpclient engine.

Drop-in surface for code written against httpx's synchronous ``Client``:
same constructor parameters (session-level), same request parameters, the
same ``Response`` helpers and exception names. Concurrency comes from
gevent, not from asyncio; there is no HTTP/2 and no transport mounting.

Not every httpx feature has an equivalent here. Per-request ``auth``,
``cookies`` and ``timeout`` are session-level settings (configure the
``Client``), and ``build_request``/``send`` are not implemented.
"""

from __future__ import annotations

import json as jsonlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, cast
from urllib.parse import urljoin

from geventhttpclient import useragent
from geventhttpclient.auth import BasicAuth, resolve_auth
from geventhttpclient.header import HeadersDataType
from geventhttpclient.requests import RequestsResponse, Session
from geventhttpclient.url import URL, ParamsDataType
from geventhttpclient.useragent import CompatRequest, FilesInput, Payload

__all__ = [
    "BasicAuth",
    "Client",
    "ConnectError",
    "ConnectTimeout",
    "DecodingError",
    "HTTPError",
    "HTTPStatusError",
    "InvalidURL",
    "NetworkError",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "RequestError",
    "Response",
    "TimeoutException",
    "TooManyRedirects",
    "TransportError",
    "WriteError",
    "WriteTimeout",
    "delete",
    "get",
    "head",
    "options",
    "patch",
    "post",
    "put",
    "request",
    "stream",
]


# ---------------------------------------------------------------------------
# Exceptions, mirroring the httpx hierarchy names
# ---------------------------------------------------------------------------


class HTTPError(Exception):
    """Base class for all errors this client raises."""

    _request: CompatRequest | None = None

    def __init__(self, message: str, *, request: CompatRequest | None = None) -> None:
        super().__init__(message)
        self._request = request

    @property
    def request(self) -> CompatRequest | None:
        return self._request


class RequestError(HTTPError):
    """An error while sending the request or processing the response."""


class HTTPStatusError(HTTPError):
    """Raised by :meth:`Response.raise_for_status` for 4xx and 5xx replies."""

    def __init__(
        self,
        message: str,
        *,
        request: CompatRequest | None = None,
        response: Response | None = None,
    ) -> None:
        super().__init__(message)
        self._request = request
        self._response = response

    @property
    def request(self) -> CompatRequest | None:
        return self._request

    @property
    def response(self) -> Response | None:
        return self._response


class TransportError(RequestError):
    """The connection failed at the transport level."""


class ConnectError(TransportError):
    """The TCP/TLS connection could not be established."""


class ReadError(TransportError):
    """The connection broke while reading the response."""


class WriteError(TransportError):
    """The connection broke while sending the request."""


class NetworkError(TransportError):
    """A network error that is neither clearly connect nor read/write."""


class TimeoutException(TransportError):
    """The operation timed out."""


class ConnectTimeout(TimeoutException):
    """Timed out while establishing the connection."""


class ReadTimeout(TimeoutException):
    """Timed out while reading the response."""


class WriteTimeout(TimeoutException):
    """Timed out while sending the request body."""


class PoolTimeout(TimeoutException):
    """Timed out waiting for a free connection from the pool."""


class TooManyRedirects(RequestError):
    """More redirects than ``max_redirects`` were followed."""


class DecodingError(RequestError):
    """The response body could not be decoded."""


class InvalidURL(HTTPError):
    """The URL could not be parsed."""


_TRANSLATIONS: tuple[tuple[type[Exception], type[HTTPError]], ...] = (
    (TimeoutError, TimeoutException),
    (useragent.BadStatusCode, HTTPStatusError),
    (useragent.RetriesExceeded, TransportError),
    (useragent.UnrewoundBodyError, TransportError),
    (useragent.UnsupportedRedirectSchemeError, TransportError),
    (ConnectionError, ConnectError),
)


def _translate(error: BaseException, request: CompatRequest | None) -> HTTPError:
    """Wrap one of the engine's errors into the httpx-named hierarchy."""
    if isinstance(error, HTTPError):
        if error._request is None:
            error._request = request
        return error
    if isinstance(error, useragent.RetriesExceeded) and "Redirection limit" in str(error):
        translated: HTTPError = TooManyRedirects(str(error), request=request)
        translated.__cause__ = error
        return translated
    for base, target in _TRANSLATIONS:
        if isinstance(error, base):
            translated = target(f"{type(error).__name__}: {error}", request=request)
            translated.__cause__ = error
            return translated
    # OSError covers socket.timeout (pre-3.10 alias), EPIPE/ECONNRESET & co.
    translated = NetworkError(f"{type(error).__name__}: {error}", request=request)
    translated.__cause__ = error
    return translated


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class Response(RequestsResponse):
    """httpx-flavored response: status predicates, httpx iterators and
    an httpx-style ``raise_for_status``."""

    _num_bytes_downloaded = 0

    @property
    def is_informational(self) -> bool:
        return 100 <= self.status_code < 200

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def has_redirect_location(self) -> bool:
        return self.is_redirect

    @property
    def is_client_error(self) -> bool:
        return 400 <= self.status_code < 500

    @property
    def is_server_error(self) -> bool:
        return 500 <= self.status_code < 600

    @property
    def is_error(self) -> bool:
        return self.is_client_error or self.is_server_error

    @property
    def charset_encoding(self) -> str | None:
        """The charset declared in the Content-Type header, httpx naming."""
        return self.encoding

    @property
    def http_version(self) -> str | None:
        """The HTTP protocol version, e.g. ``"HTTP/1.1"``."""
        try:
            return self._response.get_http_version()
        except Exception:  # noqa: BLE001 - unavailable before headers exist
            return None

    @property
    def num_bytes_downloaded(self) -> int:
        return self._num_bytes_downloaded

    def read(self, n: int | None = None) -> bytes:
        data = super().read(n)
        self._num_bytes_downloaded += len(data)
        return data

    @property
    def content(self) -> bytes:
        data = super().content
        # super().content drains the stream regardless of how it counts; the
        # downloaded total is the full body once .content was accessed
        self._num_bytes_downloaded = len(data)
        return data

    def iter_bytes(self, chunk_size: int = 1) -> Iterator[bytes]:
        """Iterate over the body in chunks of raw (still compressed) bytes.

        httpx iterates *uncompressed* bytes here; we stream the payload as
        received, matching :meth:`requests.Response.iter_content`.
        """
        return self._iter_content_bytes(chunk_size)

    def iter_raw(self, chunk_size: int = 1) -> Iterator[bytes]:
        """Iterate over the raw received bytes, exactly as they arrived."""
        return self._iter_content_bytes(chunk_size)

    def iter_text(self, chunk_size: int = 1, encoding: str | None = None) -> Iterator[str]:
        """Iterate over the body decoded to str in ``chunk_size`` chunks."""
        return (
            self._iter_content_decoded(chunk_size)
            if encoding is None
            else (self._iter_text_with_encoding(chunk_size, encoding))
        )

    def _iter_text_with_encoding(self, chunk_size: int, encoding: str) -> Iterator[str]:
        from codecs import getincrementaldecoder

        decoder = getincrementaldecoder(encoding)(errors="replace")
        for chunk in self._iter_content_bytes(chunk_size):
            yield decoder.decode(chunk)

    def raise_for_status(self) -> Response:  # type: ignore[override]  # httpx returns the response
        """Raise :class:`HTTPStatusError` for 4xx and 5xx replies.

        The exception carries ``.request`` and ``.response``; on success the
        response itself is returned, like httpx.
        """
        if self.is_error:
            message = f"Client error '{self.status_code} {self.reason}' for url '{self.url}'"
            if self.is_server_error:
                message = f"Server error '{self.status_code} {self.reason}' for url '{self.url}'"
            raise HTTPStatusError(message, request=self.request, response=self)
        return self


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client(Session):
    """httpx-style synchronous client on the geventhttpclient engine.

    Session-level configuration follows httpx: ``base_url``, ``params``,
    ``auth`` (``BasicAuth`` or a ``(username, password)`` tuple),
    ``follow_redirects`` (default ``False``, like httpx) and ``timeout``.
    Per-request ``cookies``, ``auth`` and ``timeout`` are not implemented;
    configure them on the client.
    """

    response_type = Response
    session_params: ParamsDataType | None
    follow_redirects: bool
    base_url: str | None
    auth_header: str | None

    def __init__(
        self,
        *,
        base_url: str | None = None,
        auth: Any = None,
        params: ParamsDataType | None = None,
        follow_redirects: bool = False,
        timeout: float | tuple[float, float] | None = None,
        http2: bool = False,
        **kw: Any,
    ) -> None:
        self.base_url = base_url.rstrip("/") if base_url else None
        self.auth_header = resolve_auth(auth)
        if params is None:
            self.session_params = None
        elif isinstance(params, Mapping):
            self.session_params = dict(params)
        else:
            # ParamsDataType also covers query-string / bytes /
            # iterable-of-tuples; carry them through unchanged.
            self.session_params = params
        self.follow_redirects = follow_redirects
        # httpx parity: ``http2=True`` opts into HTTP/2 with automatic
        # ALPN-based fallback to HTTP/1.1 (same kwarg name as httpx).
        kw["http2"] = http2
        if isinstance(timeout, tuple):
            if len(timeout) == 2:
                kw.setdefault("connection_timeout", timeout[0])
                kw.setdefault("network_timeout", timeout[1])
            else:
                raise ValueError("timeout tuple must be (connect, read)")
        elif timeout is not None:
            kw.setdefault("network_timeout", timeout)
        super().__init__(**kw)

    def _resolve_url(self, url: str | URL) -> str:
        resolved = str(url)
        if self.base_url and not resolved.lower().startswith(("http://", "https://", "//")):
            resolved = urljoin(self.base_url + "/", resolved.lstrip("/"))
        return resolved

    def _merge_params(
        self,
        request_params: ParamsDataType | None,
    ) -> ParamsDataType | None:
        if self.session_params and request_params:
            # ``ParamsDataType`` is too wide for ``dict.update`` (it
            # includes ``str`` / ``bytes`` / ``Iterable[tuple]`` which
            # have no ``__getitem__``); we only merge when both sides
            # are mappings.
            if isinstance(self.session_params, Mapping) and isinstance(request_params, Mapping):
                merged = dict(self.session_params)
                merged.update(request_params)
                return merged
            return request_params or self.session_params
        return request_params or self.session_params

    def request(  # type: ignore[override]  # httpx takes keyword-only arguments
        self,
        method: str,
        url: str | URL,
        *,
        params: ParamsDataType | None = None,
        content: Payload = None,
        data: Payload = None,
        headers: HeadersDataType | None = None,
        cookies: Any = None,
        files: FilesInput | None = None,
        json: Any = None,
        follow_redirects: bool | None = None,
        auth: Any = None,
        timeout: float | tuple[float, float] | None = None,
    ) -> Response:
        if cookies is not None:
            raise NotImplementedError(
                "per-request cookies are not supported; configure them on the client"
            )
        if timeout is not None:
            raise NotImplementedError(
                "per-request timeouts are not supported; configure a timeout on the client"
            )
        if auth is not None:
            raise NotImplementedError(
                "per-request auth is not supported; configure auth on the client"
            )
        if follow_redirects is None:
            follow_redirects = self.follow_redirects

        payload: Payload | None = data if data is not None else content
        if json is not None:
            if payload is not None:
                raise ValueError("Can send either data/content or json, not both at once")
            payload = jsonlib.dumps(json)
            headers = dict(headers) if headers else {}
            headers["Content-Type"] = "application/json"

        if self.auth_header:
            headers = dict(headers) if headers else {}
            headers.setdefault("Authorization", self.auth_header)

        try:
            response = cast(
                Response,
                super().request(
                    method,
                    self._resolve_url(url),
                    params=self._merge_params(params),
                    headers=headers,
                    files=files,
                    json=None,
                    data=payload,
                    allow_redirects=bool(follow_redirects),
                ),
            )
        except HTTPError:
            raise
        except BaseException as error:  # noqa: BLE001 - translated into the hierarchy below
            translated = _translate(error, None)
            if translated._request is None:
                translated._request = getattr(error, "request", None)
            raise translated from translated.__cause__
        return response

    @contextmanager
    def stream(self, method: str, url: str, **kw: Any) -> Iterator[Response]:
        """Request with a streaming response, closed on context exit."""
        response = self.request(method, url, **kw)
        try:
            yield response
        finally:
            response.close()

    def get(self, url: str | URL, **kw: Any) -> Response:
        return self.request("GET", url, **kw)

    def options(self, url: str | URL, **kw: Any) -> Response:
        return self.request("OPTIONS", url, **kw)

    def head(self, url: str | URL, **kw: Any) -> Response:
        return self.request("HEAD", url, **kw)

    def post(
        self,
        url: str | URL,
        data: Payload = None,
        json: Any = None,
        **kw: Any,
    ) -> Response:
        return self.request("POST", url, data=data, json=json, **kw)

    def put(
        self,
        url: str | URL,
        data: Payload = None,
        **kw: Any,
    ) -> Response:
        return self.request("PUT", url, data=data, **kw)

    def patch(
        self,
        url: str | URL,
        data: Payload = None,
        **kw: Any,
    ) -> Response:
        return self.request("PATCH", url, data=data, **kw)

    def delete(self, url: str | URL, **kw: Any) -> Response:
        return self.request("DELETE", url, **kw)

    @property
    def is_closed(self) -> bool:
        return self._closed if hasattr(self, "_closed") else False

    def close(self) -> None:
        self._closed = True
        super().close()


# ---------------------------------------------------------------------------
# Top-level API, mirroring httpx.get & friends with a lazily shared client
# ---------------------------------------------------------------------------

_client: Client | None = None


def _get_client() -> Client:
    global _client
    if _client is None:
        _client = Client(follow_redirects=True)
    return _client


def request(method: str, url: str, **kw: Any) -> Response:
    return _get_client().request(method, url, **kw)


def get(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().get(url, **kw))


def post(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().post(url, **kw))


def put(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().put(url, **kw))


def patch(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().patch(url, **kw))


def delete(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().delete(url, **kw))


def head(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().head(url, **kw))


def options(url: str, **kw: Any) -> Response:
    return cast(Response, _get_client().options(url, **kw))


@contextmanager
def stream(method: str, url: str, **kw: Any) -> Iterator[Response]:
    with _get_client().stream(method, url, **kw) as response:
        yield response
