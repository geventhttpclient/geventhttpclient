import binascii
import errno
import json as jsonlib
import os
import socket
import ssl
import time
import urllib.request
import zlib
from collections.abc import Iterable, Iterator, Mapping, MutableMapping
from datetime import timedelta
from http.cookiejar import CookieJar
from types import TracebackType
from typing import IO, Any, ClassVar, Final, Literal, Never, Self, cast, overload
from urllib.parse import urlencode

import brotli
import gevent

from geventhttpclient.client import IDEMPOTENT_METHODS, HTTPClient, HTTPClientPool
from geventhttpclient.header import Headers, HeadersDataType, parse_content_type_charset
from geventhttpclient.http2.response import HTTP2Response, HTTP2SocketResponseBridge
from geventhttpclient.response import HTTPSocketPoolResponse, HTTPSocketResponse
from geventhttpclient.url import URL, ParamsDataType, to_key_val_list

# Request payloads are passed through to the client untouched, which accepts
# mappings, strings, bytes and file like objects, depending on the content type.
# _make_request normalises mappings and strings into bytes before the client
# ever sees them.
Payload = (
    str
    | bytes
    | bytearray
    | memoryview
    | MutableMapping[str, Any]
    | IO[bytes]
    | Iterable[bytes]
    | None
)

FilesInput = Mapping[str, Any] | Iterable[tuple[str, Any]]

# The stdlib jar, fed our own urllib.request compatible request and response
# objects, which the stdlib annotations do not accept.
CookieJarLike = CookieJar | None


class ConnectionError(Exception):
    url: str | URL | None
    text: str
    kw_text: str

    def __init__(self, url: str | URL | None, *args: Any, **kw: Any) -> None:
        self.url = url
        self.__dict__.update(kw)
        if args and isinstance(args[0], str):
            self.text = args[0] if len(args) == 1 else args[0] + ": " + str(args[1:])
        else:
            self.text = str(args[0]) if len(args) == 1 else ""
        if kw:
            self.text += ", " if self.text else ""
            self.kw_text = ", ".join(f"{key}={val}" for key, val in kw.items())
            self.text += self.kw_text
        else:
            # keep a message carried in args intact; only the kw appendix is empty
            self.kw_text = ""

    def __str__(self) -> str:
        if self.text:
            return f"URL {self.url}: {self.text}"
        else:
            return f"URL {self.url}"

    def __repr__(self) -> str:
        repr_str = super().__repr__()
        if self.kw_text:
            return repr_str.replace(")", f", {self.kw_text})")
        return repr_str


class RetriesExceeded(ConnectionError):
    pass


class BadStatusCode(ConnectionError):
    # populated by raise_for_status() and the urlopen status check: the
    # failing status code plus the response/request that caused it.
    # The annotations are only for documentation; mypy cannot see
    # the dynamic attribute writes inside the except branches, so
    # the assignments below keep a # type: ignore marker.
    code: int = 0
    response: "CompatResponse | None"
    request: "CompatRequest | None"


class EmptyResponse(ConnectionError):
    pass


class UnrewoundBodyError(ConnectionError):
    pass


class UnsupportedRedirectSchemeError(ConnectionError):
    pass


class CompatRequest(urllib.request.Request):
    """urllib.request.Request compatible request class.
    See also: http://docs.python.org/library/cookielib.html

    Deliberate deviations from the base class: headers is our case-insensitive
    Headers mapping instead of a plain dict, add_unredirected_header merges
    into it so that cookies survive our in-place redirects, header_items reads
    the joined values instead of the raw dict internals, and payload is the
    single body store that data exposes.
    """

    url_split: URL
    original_host: str
    original_origin: tuple[str, str, int | None]
    headers: Headers  # type: ignore[assignment]
    payload: Payload
    # the base class allows None until the opener picks a method, ours is final
    method: str

    def __init__(
        self,
        url: str | URL,
        method: str = "GET",
        headers: Headers | None = None,
        payload: Payload = None,
        params: ParamsDataType | None = None,
    ) -> None:
        self.set_url(url, params=params)
        self.original_host = self.url_split.host
        self.original_origin = (
            self.url_split.scheme,
            self.url_split.host,
            self.url_split.port,
        )
        self.method = method.upper()
        # None is accepted for backwards compatibility with callers which never
        # touch the headers. Every path reading them requires a Headers object.
        self.headers = headers  # type: ignore[assignment]
        self.unredirected_hdrs = {}
        self.origin_req_host = self.original_host
        self.unverifiable = False
        self._tunnel_host = None
        self.payload = payload

    @property
    def full_url(self) -> str:
        return self.url

    @full_url.setter
    def full_url(self, url: str) -> None:
        self.set_url(url)

    @property  # type: ignore[override]
    def data(self) -> Payload:
        return self.payload

    @data.setter
    def data(self, value: Payload) -> None:
        self.payload = value

    def set_url(self, url: str | URL, params: ParamsDataType | None = None) -> None:
        if isinstance(url, URL):
            self.url = str(url)
            self.url_split = url
        else:
            self.url = url
            self.url_split = URL(self.url, params=params)
        # the base class keeps these as plain attributes, parsed from the URL;
        # host is the netloc there, port and userinfo included, get_host is our
        # own reading and stays without both
        self.type = self.url_split.scheme
        self.host = self.url_split.netloc
        self.selector = self.url_split.request_uri or "/"

    def get_host(self) -> str:
        return self.url_split.host

    def get_type(self) -> str:
        return self.url_split.scheme

    def get_origin_req_host(self) -> str:
        return self.original_host

    def is_unverifiable(self) -> bool:
        """RFC 2965 section 3.3: True once the request has been through a
        redirect. urllib.request marks redirected requests the same way, so
        the stdlib cookie policy treats cookies set along the chain as
        unverifiable - blocked by strict policies, ignored by the
        permissive defaults."""
        return self.unverifiable

    def add_unredirected_header(self, key: str, val: str) -> None:
        # the base class parks these in a dict our client never reads, so they
        # would silently vanish; ours go into the headers proper
        self.headers.add(key, val)

    def header_items(self) -> list[tuple[str, str]]:
        # the base class merges the raw dict internals of Headers, which would
        # leak the lowercased keys and the internal tuples
        return list(self.headers.items())

    def _drop_payload(self) -> None:
        if self.method != "HEAD":
            # RFC 9110 section 15.4: an automatic redirect changes the
            # request method according to the redirecting status code's
            # semantics. That rewrites body-carrying methods to GET; HEAD
            # stays HEAD, like requests and browsers keep it.
            self.method = "GET"
        self.payload = None
        for item in ("content-length", "content-type", "content-encoding"):
            self.headers.discard(item)

    def _rewind_payload(self) -> None:
        """307/308 keep method and payload: a seekable body is rewound so the
        resent request carries the full body again. After the first send the
        stream sits at its end and the redirected request would ship an empty
        body under the original length, leaving the server waiting for bytes
        that never arrive."""
        if self.payload is None:
            return
        seek = getattr(self.payload, "seek", None)
        if seek is not None:
            try:
                seek(0)
            except OSError as e:
                # e.g. a BufferedReader on a pipe (subprocess.Popen.stdout):
                # the seek attribute exists, seek(0) fails with ESPIPE - the
                # body is not rewindable either
                raise UnrewoundBodyError(
                    self.url, "payload cannot be rewound and resent after a redirect"
                ) from e
        elif not isinstance(self.payload, (bytes, bytearray, memoryview, str, Mapping)):
            raise UnrewoundBodyError(
                self.url, "payload cannot be rewound and resent after a redirect"
            )

    def _drop_cookies(self) -> None:
        for item in ("cookie", "cookie2"):
            self.headers.discard(item)

    def redirect(self, code: int, location: str) -> None:
        """Modify the request inplace to point to the new location"""
        new_url = self.url_split.redirect(location)
        # RFC 9110 section 15.4 has the user agent resolve Location within
        # the HTTP context it is already in; a redirect to ftp:, data: or a
        # custom scheme is outside of it. HTTPClient.from_url would silently
        # degrade anything but https to plain http, so refuse instead of
        # downgrading.
        if new_url.scheme not in ("http", "https"):
            raise UnsupportedRedirectSchemeError(
                self.url, f"refusing to follow redirect to {new_url.scheme!r} URL"
            )
        self.set_url(new_url)
        if code in (301, 302, 303):
            self._drop_payload()
        else:
            # 307/308 keep the payload: rewind what was sent so far, the
            # whole body belongs to the redirected request again
            self._rewind_payload()
        self._drop_cookies()
        if not self._is_same_origin():
            self.headers.discard("authorization")
        # RFC 2965 section 3.3: a request produced by a server redirect is
        # unverifiable from the user's perspective. urllib.request marks
        # redirected requests the same way, and strict cookie policies use
        # the flag to refuse cookies set along the chain. Cookies of the
        # redirecting response itself were extracted before this point and
        # stay verifiable.
        self.unverifiable = True

    def _is_same_origin(self) -> bool:
        """The RFC 6454 origin (scheme, host, port) of the redirect target
        against the origin this request was created with."""
        url = self.url_split
        return (url.scheme, url.host, url.port) == self.original_origin


class CompatResponse:
    """Adapter for urllib3-style responses."""

    __slots__ = (
        "_cached_content",
        "_elapsed",
        "_history",
        "_request",
        "_response",
        "_sent_request",
        "headers",
    )

    _response: HTTPSocketResponse
    _request: CompatRequest | None
    _sent_request: str | None
    headers: Headers
    _cached_content: bytes
    _elapsed: float | None
    _history: list["CompatResponse"]

    def __init__(
        self,
        ghc_response: HTTPSocketResponse,
        request: CompatRequest | None = None,
        sent_request: str | None = None,
    ) -> None:
        self._response = ghc_response
        self._request = request
        self._sent_request = sent_request
        self.headers = self._response._headers_index
        self._elapsed = None
        self._history = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()

    @property
    def status_code(self) -> int:
        """HTTP status code as plain integer"""
        return self._response.get_code()

    @property
    def elapsed(self) -> timedelta | None:
        """Time between sending the request and receiving the response
        headers, as measured by the session layer (``None`` when unset)."""
        if self._elapsed is None:
            return None
        return timedelta(seconds=self._elapsed)

    @property
    def history(self) -> list["CompatResponse"]:
        """The responses of the redirect chain that led to this response,
        oldest first (empty when the request was served directly)."""
        return self._history

    def __len__(self) -> int:
        """The content lengths as declared from the headers"""
        # a chunked response declares no length at all; the protocol level check
        # in len() still raises for it, as it always did
        return self._response.length  # type: ignore[return-value]

    def info(self) -> Headers:
        """Adaption to http.client."""
        return self.headers

    def __iter__(self) -> Iterator[bytes]:
        return iter(self._response)

    def read(self, n: int | None = None) -> bytes:
        """Read n bytes from the response body"""
        return self._response.read(n)

    def readline(self) -> bytes:
        # the wrapped response defaults to the header delimiter b"\r\n"
        return self._response.readline()

    def release(self) -> None:
        return self._response.release()

    def unzipped(self, gzip: bool = True, br: bool = False) -> bytes:
        bodystr = self._response.read()
        if gzip:
            return zlib.decompress(bodystr, 16 + zlib.MAX_WBITS)
        elif br:
            return cast(bytes, brotli.decompress(bodystr))
        else:
            # zlib only provides the zlib compress format, not the deflate format;
            # so on top of all there's this workaround:
            try:
                return zlib.decompress(bodystr, -zlib.MAX_WBITS)
            except zlib.error:
                return zlib.decompress(bodystr)

    @property
    def content(self) -> bytes:
        """Unzips if necessary and buffers the received body. Careful with large files!"""
        try:
            return self._cached_content
        except AttributeError:
            self._cached_content = self._content()
            return self._cached_content

    def _content(self) -> bytes:
        try:
            content_encoding = self.headers.getlist("content-encoding")[0].lower()
        except IndexError:
            # No content-encoding header set
            content_encoding = "identity"

        if content_encoding == "gzip":
            ret = self.unzipped(gzip=True)
        elif content_encoding == "deflate":
            ret = self.unzipped(gzip=False)
        elif content_encoding == "identity":
            ret = self._response.read()
        elif content_encoding == "br":
            ret = self.unzipped(gzip=False, br=True)
        elif content_encoding == "compress":
            raise ValueError(f"Compression type not supported: {content_encoding}")
        else:
            raise ValueError(f"Unknown content encoding: {content_encoding}")

        self.release()
        return ret

    @property
    def text(self) -> bytes | str:
        """Decoded body for text content types, raw bytes otherwise. Unlike
        requests.Response.text this does not decode non-text responses, to
        avoid mangling binary payloads."""
        if not self.content:
            return ""

        try:
            content_type = self.headers.getlist("content-type")[0]
        except IndexError:
            # No content-type header set, let's hope for the best
            return self.content.decode()

        if content_type.lower().startswith("text"):
            codec = parse_content_type_charset(content_type) or "utf-8"
            return self.content.decode(codec)
        return self.content

    def json(self) -> Any:
        return jsonlib.load(self)

    # the stuff only for urllib3

    @property
    def status(self) -> str:
        """HTTP status for urllib3"""
        return str(self.status_code)

    @property
    def data(self) -> bytes:
        """Content for urllib3"""
        return self.content

    @property
    def stream(self) -> HTTPSocketResponse:
        """Readable stream for urllib3"""
        return self._response

    def isclosed(self) -> bool:
        """Closed status for urllib3"""
        return self._response.message_complete


# Status codes whose Location header the client follows. Same set requests
# calls a redirect, and the default for UserAgent.redirect_response_codes.
REDIRECT_RESPONSE_CODES: Final[frozenset[int]] = frozenset([301, 302, 303, 307, 308])

# Subset of REDIRECT_RESPONSE_CODES whose Location header marks a permanent
# move rather than a temporary one. Used by the requests-style surface for
# Response.is_permanent_redirect.
PERMANENT_REDIRECT_RESPONSE_CODES: Final[frozenset[int]] = frozenset([301, 308])


class UserAgent:
    response_type: ClassVar[type[CompatResponse]] = CompatResponse
    request_type: ClassVar[type[CompatRequest]] = CompatRequest
    valid_response_codes: ClassVar[frozenset[int]] = frozenset([200, 206, 301, 302, 303, 307, 308])
    redirect_response_codes: ClassVar[frozenset[int]] = REDIRECT_RESPONSE_CODES

    max_redirects: int
    max_retries: int
    retry_delay: float
    default_headers: Headers
    cookiejar: CookieJarLike
    clientpool: HTTPClientPool

    def __init__(
        self,
        max_redirects: int = 3,
        max_retries: int = 3,
        retry_delay: float = 0,
        cookiejar: CookieJarLike = None,
        headers: HeadersDataType | None = None,
        *,
        insecure: bool = False,
        http2: bool = False,
        **kw: Any,
    ) -> None:
        self.max_redirects = int(max_redirects)
        self.max_retries = int(max_retries)
        self.retry_delay = retry_delay
        self.default_headers = HTTPClient.DEFAULT_HEADERS.copy()
        if headers:
            self.default_headers.update(headers)
        self.cookiejar = cookiejar
        # Forward the explicit args so HTTPClient / HTTPClientPool pick
        # them up; previously only ``**kw`` carried them, which made
        # ``insecure=True`` silently drop on the floor if the caller
        # did not also set the other kwargs. Higher-level clients
        # (``Session``, ``httpx.Client``) translate their own
        # ``follow_redirects=False`` into ``max_redirects=0`` on
        # this constructor.
        if insecure:
            kw["insecure"] = True
        # httpx-style opt-in switch: when set, the HTTPClientPool
        # creates HTTPClient instances with ``http2=True``.
        # The default stays HTTP/1.1.
        self.http2 = http2
        self.clientpool = HTTPClientPool(http2=http2, **kw)

    def close(self) -> None:
        self.clientpool.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def _may_retry(self, request: CompatRequest) -> bool:
        """RFC 9110 section 9.2.2: a client SHOULD NOT automatically retry a
        request with a non-idempotent method - the previous behavior retried
        POST and PATCH on timeout, EPIPE, ECONNRESET and empty responses,
        which can execute such a request twice."""
        return request.method in IDEMPOTENT_METHODS

    def _verify_status(self, status_code: int, url: str | URL | None = None) -> None:
        """Hook for subclassing"""
        if status_code not in self.valid_response_codes:
            raise BadStatusCode(url, code=status_code)

    def _handle_error(self, e: BaseException, url: str | URL | None = None) -> BaseException:
        """Hook for subclassing. Raise the error to interrupt further retrying,
        return it to continue retries and save the error, when retries
        exceed the limit.
        Temporary errors should be swallowed here for automatic retries.
        """
        if (
            isinstance(e, (socket.timeout, gevent.Timeout))
            or isinstance(e, socket.error)
            and e.errno
            in {
                errno.ETIMEDOUT,
                errno.ENOLINK,
                errno.ENOENT,
                errno.EPIPE,
            }
            or isinstance(e, ssl.SSLError)
            and "read operation timed out" in str(e)
            or isinstance(e, EmptyResponse)
        ):
            return e
        raise e.with_traceback(e.__traceback__)

    def _handle_retries_exceeded(
        self, url: str | URL, last_error: BaseException | None = None
    ) -> Never:
        """Hook for subclassing"""
        raise RetriesExceeded(url, self.max_retries, original=last_error)

    @overload
    def urlopen(
        self,
        url: str | URL,
        method: str = "GET",
        headers: HeadersDataType | None = None,
        payload: Payload = None,
        to_string: Literal[False] = False,
        debug_stream: IO[str] | None = None,
        params: ParamsDataType | None = None,
        max_retries: int | None = None,
        max_redirects: int | None = None,
        files: FilesInput | None = None,
        **kw: Any,
    ) -> CompatResponse: ...

    @overload
    def urlopen(
        self,
        url: str | URL,
        method: str = "GET",
        headers: HeadersDataType | None = None,
        payload: Payload = None,
        to_string: Literal[True] = True,
        debug_stream: IO[str] | None = None,
        params: ParamsDataType | None = None,
        max_retries: int | None = None,
        max_redirects: int | None = None,
        files: FilesInput | None = None,
        **kw: Any,
    ) -> bytes: ...

    def urlopen(
        self,
        url: str | URL,
        method: str = "GET",
        headers: HeadersDataType | None = None,
        payload: Payload = None,
        to_string: bool = False,
        debug_stream: IO[str] | None = None,
        params: ParamsDataType | None = None,
        max_retries: int | None = None,
        max_redirects: int | None = None,
        files: FilesInput | None = None,
        **kw: Any,
    ) -> CompatResponse | bytes:
        """Open a URL, do retries and redirects and verify the status code"""
        # POST or GET parameters can be passed in **kw
        req_headers = self.default_headers.copy()
        if headers:
            req_headers.update(headers)
        if kw:
            if method.upper() == "POST":
                if payload is None:
                    payload = kw
                elif isinstance(payload, dict):
                    payload.update(kw)
            else:
                if params is None:
                    params = kw
                elif isinstance(params, dict):
                    params.update(kw)
        req = _make_request(
            url,
            method,
            headers=req_headers,
            payload=payload,
            params=params,
            files=files,
            request_type=self.request_type,
        )
        max_retries = int(max_retries) if max_retries is not None else self.max_retries
        max_redirects = int(max_redirects) if max_redirects is not None else self.max_redirects
        history: list[CompatResponse] = []

        for retry in range(max_retries + 1):
            if retry > 0 and self.retry_delay:
                # Don't wait the first time and skip if no delay specified
                gevent.sleep(self.retry_delay)
            for _ in range(max_redirects + 1):
                if self.cookiejar is not None:
                    self.cookiejar.add_cookie_header(req)

                try:
                    started = time.monotonic()
                    resp = self._urlopen(req)
                    resp._elapsed = time.monotonic() - started
                except gevent.GreenletExit:
                    raise
                except BaseException as e:  # noqa: BLE001
                    e.request = req  # type: ignore[attr-defined]
                    last_error = self._handle_error(e, url=req.url)
                    # _handle_error returning means it wants this retried
                    if not self._may_retry(req):
                        raise self._handle_retries_exceeded(url, last_error=last_error)
                    break  # Continue with next retry

                # We received a response
                if debug_stream is not None:
                    debug_stream.write(
                        self._conversation_str(req.url, resp, payload=req.payload) + "\n\n"
                    )

                if self.cookiejar is not None:
                    self.cookiejar.extract_cookies(resp, req)  # type: ignore[arg-type]

                try:
                    self._verify_status(resp.status_code, url=req.url)
                except Exception as e:  # noqa: BLE001
                    # Basic transmission successful, but not the wished result
                    # Let's collect some debug info
                    e.response = resp  # type: ignore[attr-defined]
                    e.request = req  # type: ignore[attr-defined]
                    e.http_log = self._conversation_str(req.url, resp, payload=req.payload)  # type: ignore[attr-defined]
                    resp.release()
                    last_error = self._handle_error(e, url=req.url)
                    break  # Continue with next retry

                redirection = resp.headers.get("location")
                if not isinstance(redirection, str):
                    redirection = None
                if (
                    max_redirects > 0
                    and resp.status_code in self.redirect_response_codes
                    and redirection
                ):
                    history.append(resp)
                    resp.release()
                    try:
                        req.redirect(resp.status_code, redirection)
                        continue
                    except Exception as e:  # noqa: BLE001
                        last_error = self._handle_error(e, url=req.url)
                        break

                if not to_string:
                    resp._history = history
                    return resp
                else:
                    # to_string added as parameter, to handle empty response
                    # bodies as error and continue retries automatically
                    try:
                        ret = resp.content
                    except Exception as e:  # noqa: BLE001
                        last_error = self._handle_error(e, url=req.url)
                        break
                    else:
                        if not ret:
                            # reusing the name bound by the except block above,
                            # which python deletes once the handler is left
                            e = EmptyResponse(url, "Empty response body received")  # type: ignore[misc]
                            if not self._may_retry(req):
                                raise self._handle_retries_exceeded(url, last_error=e)  # type: ignore[misc]
                            last_error = self._handle_error(e, url=req.url)  # type: ignore[misc]
                            break
                        else:
                            return ret
            else:
                e = RetriesExceeded(url, f"Redirection limit reached ({self.max_redirects})")  # type: ignore[misc]
                last_error = self._handle_error(e, url=url)  # type: ignore[misc]
        return self._handle_retries_exceeded(url, last_error=last_error)

    def _urlopen(self, request: CompatRequest) -> CompatResponse:
        client = self.clientpool.get_client(request.url_split)
        # httpx-style auto-fallback (review_http2_3.md H1 + Phase 6):
        # the user opted into ``http2=True`` on the client, so
        # we route through ``_urlopen_h2`` whenever the request uses
        # TLS. The h2 path performs its own ALPN-aware fallback to
        # HTTP/1.1 when the peer did not negotiate ``h2``; no
        # per-request ``version`` knob is exposed here.
        if client.http2 and client.ssl:
            return self._urlopen_h2(request, client)
        resp = client.request(
            request.method,
            request.url_split.quoted_uri,
            # _make_request already normalised mappings and strings into bytes
            body=request.payload,  # type: ignore[arg-type]
            headers=request.headers,
        )
        return self.response_type(resp, request=request, sent_request=resp._sent_request)

    def _urlopen_h2(
        self,
        request: CompatRequest,
        client: HTTPClient,
    ) -> CompatResponse:
        """HTTP/2 path. Synchronous; uses ``HTTPClient.request_h2``.

        The h2 session lives inside ``HTTP2ConnectionPool`` so redirects
        reuse the same multiplexed connection when they target the
        same origin. Cross-origin redirects fall through to the
        HTTPClientPool and create a new h2 session.

        ``version`` controls ALPN dispatch: ``"auto"`` falls back to
        HTTP/1.1 if the peer did not negotiate ``h2``; ``"2"`` forces
        HTTP/2 and raises otherwise; ``"1.1"`` short-circuits to the
        HTTP/1.1 path before opening an h2 session.
        """
        payload = request.payload
        # Match the HTTP/1.1 path's payload normalisation in
        # :func:`_make_request`: ``str`` and ``dict`` are converted to
        # ``bytes`` (with a matching Content-Type header the caller
        # already set via ``_make_request``). Iterables are buffered
        # into a single ``bytes`` so the h2 client sends a single
        # DATA frame instead of a stream (review_http2_3.md H1).
        body: bytes | None = None
        if isinstance(payload, (bytes, bytearray, memoryview)):
            body = bytes(payload)
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        elif isinstance(payload, dict):
            # ``urlencode`` is already imported at module scope; use
            # it to mirror the HTTP/1.1 form-encoding semantics.
            body = urlencode(payload).encode("utf-8")
        elif isinstance(payload, Iterable):
            buf = bytearray()
            for chunk in payload:
                if isinstance(chunk, str):
                    buf.extend(chunk.encode("utf-8"))
                else:
                    buf.extend(chunk)
            body = bytes(buf)
        elif payload is not None and hasattr(payload, "read"):
            body = payload.read()
        # Per-request timeout (review_http2_3.md H2): default to the
        # connection pool's network_timeout when the request did not
        # set one explicitly. Without a deadline, ``request_h2``'s
        # pump loop waits forever for a response that never comes.
        timeout = getattr(request, "timeout", None)
        if timeout is None:
            timeout = client._connection_pool.network_timeout
        result = client.request_h2(
            request.method,
            request.url_split.quoted_uri,
            body=body,
            headers=request.headers,
            timeout=timeout,
        )
        # ``request_h2`` returns either an ``HTTP2ResponseHandle`` (the
        # h2 path) or an ``HTTPSocketPoolResponse`` (auto-fallback
        # when the peer chose http/1.1 in the ALPN handshake).
        if isinstance(result, HTTPSocketPoolResponse):
            return self.response_type(
                result,
                request=request,
                sent_request=result._sent_request,
            )
        h2_resp = HTTP2Response(result)
        # The h2 path never sees the raw request head (the framing is
        # internal to nghttp2); the bridge carries a synthesised
        # request line so ``_conversation_str`` debug output works.
        bridge = HTTP2SocketResponseBridge(
            h2_resp,
            sent_request=(f"{request.method} {request.url_split.quoted_uri} HTTP/2.0\r\n"),
        )
        return self.response_type(bridge, request=request)  # type: ignore[arg-type]

    @classmethod
    def _conversation_str(
        cls,
        url: str,
        resp: CompatResponse,
        payload: Payload = None,
        encoding: str = "utf-8",
    ) -> str:
        header_str = "\n".join(f"{key}: {val}" for key, val in resp.headers.items())
        ret = "REQUEST: " + url + "\n" + resp._sent_request  # type: ignore[operator]
        if payload:
            if isinstance(payload, bytes):
                try:
                    ret += payload.decode(encoding) + "\n\n"
                except UnicodeDecodeError:
                    ret += "UnicodeDecodeError" + "\n\n"
            elif isinstance(payload, str):
                ret += payload + "\n\n"
        ret += (
            "RESPONSE: "
            + resp._response.version
            + " "
            + str(resp.status_code)
            + "\n"
            + header_str
            + "\n\n"
            + resp.content.decode(encoding)
        )
        return ret

    def download(
        self,
        url: str | URL,
        fpath: str | os.PathLike[str],
        chunk_size: int = 16 * 1024,
        resume: bool = False,
        **kw: Any,
    ) -> CompatResponse:
        kw.pop("to_string", None)
        headers = kw.pop("headers", {})
        headers["Connection"] = "Keep-Alive"
        if resume and os.path.isfile(fpath):
            offset = os.path.getsize(fpath)
        else:
            offset = 0

        for _ in range(self.max_retries + 1):
            if offset:
                headers["Range"] = f"bytes={offset}-"
                resp = self.urlopen(url, headers=headers, **kw)
                cr = resp.headers.get("Content-Range")
                if (
                    resp.status_code != 206
                    or not cr
                    or not cr.startswith("bytes")
                    or not cr.split(None, 1)[1].startswith(str(offset))
                ):
                    resp.release()
                    offset = 0
            if not offset:
                if "Range" in headers:
                    del headers["Range"]
                resp = self.urlopen(url, headers=headers, **kw)

            with open(fpath, "ab" if offset else "wb") as f:
                if offset:
                    f.seek(offset, os.SEEK_SET)
                try:
                    data = resp.read(chunk_size)
                    with resp:
                        while data:
                            f.write(data)
                            data = resp.read(chunk_size)
                except BaseException as e:  # noqa: BLE001
                    self._handle_error(e, url=url)
                    if resp.headers.get("accept-ranges") == "bytes":
                        # Only if this header is set, we can fall back to partial download
                        offset = f.tell()
                    continue
            # All done, break outer loop
            break
        else:
            # `e` is only ever bound by the handler above, which python deletes
            # once it is left; reaching this branch therefore means `e` is gone
            self._handle_retries_exceeded(url, last_error=e)  # type: ignore[misc]
        return cast(CompatResponse, resp)

    def _make_request(self, *args: Any, **kw: Any) -> CompatRequest:
        """Build a request for this agent, without sending it.

        The work happens in the module level :func:`_make_request`, which takes
        the very same arguments; this method only adds the request_type of the
        agent it is called on.  Packages built on top of us create requests
        through here, so these arguments have to keep working.
        """
        kw.setdefault("request_type", self.request_type)
        return _make_request(*args, **kw)


def _make_request(
    url: str | URL,
    method: str = "GET",
    headers: Headers | None = None,
    payload: Payload = None,
    params: ParamsDataType | None = None,
    files: FilesInput | None = None,
    request_type: type[CompatRequest] = CompatRequest,
) -> CompatRequest:
    # callers that have no headers at all pass None
    if headers is None:
        headers = Headers()

    # Adjust headers depending on payload content
    content_type = headers.get("content-type")
    if files:
        payload, content_type = _encode_multipart_formdata(files, payload)
        headers["content-type"] = content_type
        headers["content-length"] = str(len(payload))
    elif payload:
        if isinstance(payload, dict):
            if not content_type:
                headers["content-type"] = "application/x-www-form-urlencoded; charset=utf-8"
            payload = urlencode(payload).encode()
            headers["content-length"] = str(len(payload))
        elif not content_type and isinstance(payload, str):
            headers["content-type"] = "text/plain; charset=utf-8"
            payload = payload.encode()
            headers["content-length"] = str(len(payload))
        elif not content_type:
            headers["content-type"] = "application/octet-stream"

    return request_type(url, method=method, headers=headers, payload=payload, params=params)


def _guess_filename(file: Any) -> str | None:  # type: ignore[return]
    """Tries to guess the filename of the given object."""
    name = getattr(file, "name", None)
    if not name or not isinstance(name, (str, bytes)):
        return  # type: ignore[return-value]
    if isinstance(name, bytes):
        name = name.decode()
    if name[0] != "<" and name[-1] != ">":
        return os.path.basename(name)


def _quote_param(value: Any) -> str:
    """Quote a Content-Disposition parameter value (HTML5 style)."""
    return str(value).replace('"', "%22")


def _multipart_part(
    boundary: bytes,
    name: str,
    data: str | bytes | bytearray,
    filename: str | None = None,
    content_type: str | None = None,
    extra_headers: HeadersDataType | None = None,
) -> bytes:
    """Render a single multipart/form-data part."""
    disposition = f'Content-Disposition: form-data; name="{_quote_param(name)}"'
    if filename is not None:
        disposition += f'; filename="{_quote_param(filename)}"'
    lines = [disposition.encode()]
    if content_type is not None:
        lines.append(f"Content-Type: {content_type}".encode())
    for key, value in to_key_val_list(extra_headers or {}):
        lines.append(f"{key}: {value}".encode())
    if isinstance(data, str):
        data = data.encode("utf-8")
    return b"".join(
        (b"--", boundary, b"\r\n", b"\r\n".join(lines), b"\r\n\r\n", bytes(data), b"\r\n")
    )


def _encode_multipart_formdata(
    files: FilesInput,
    data: Payload,
) -> tuple[bytes, str]:
    """
    Build the body for a multipart/form-data request.

    Will successfully encode files when passed as a dict or a list of
    tuples. Order is retained if data is a list of tuples but arbitrary
    if parameters are supplied as a dict.

    The tuples may be
    2-tuples (filename, fileobj),
    3-tuples (filename, fileobj, contentype),
    4-tuples (filename, fileobj, contentype, custom_headers) or
    5-tuples (filename, fileobj, contentype, custom_headers, custom boundary).

    example:
    files = {'file': ('report.xls', body, 'application/vnd.ms-excel', {'Expires': '0'}, 'custom_boundary')}

    """

    if not files:
        raise ValueError("Files must be provided.")
    elif isinstance(data, (str, bytes)):
        raise ValueError("Data must not be a string.")

    file_items = to_key_val_list(files or {})
    # a custom boundary can be given in the 5-tuple form of a file
    boundary = next(
        (item[4] for _, item in file_items if isinstance(item, (tuple, list)) and len(item) >= 5),
        None,
    )
    if boundary is None:
        boundary = binascii.hexlify(os.urandom(16)).decode("ascii")
    boundary_bytes = boundary.encode("ascii")

    parts = []
    # str and bytes payloads are rejected above, everything usable here is a
    # mapping or a list of tuples; the remaining payload kinds fail in the loop
    for field, val in to_key_val_list(data or {}):  # type: ignore[arg-type]
        if isinstance(val, (str, bytes)) or not hasattr(val, "__iter__"):
            val = [val]
        for v in val:
            if v is not None:
                if isinstance(field, bytes):
                    field = field.decode("utf-8")
                if not isinstance(v, bytes):
                    v = str(v)
                parts.append(_multipart_part(boundary_bytes, field, v))

    for k, v in file_items:
        # support for explicit filename
        ft = None
        fh = None
        if isinstance(v, (tuple, list)):
            if len(v) == 2:
                fn, fp = v
            elif len(v) == 3:
                fn, fp, ft = v
            elif len(v) == 4:
                fn, fp, ft, fh = v
            else:
                # strict unpacking keeps the ValueError for wrong lengths;
                # the boundary was already determined above and must not change
                fn, fp, ft, fh, _ = v
        else:
            fn = _guess_filename(v) or k
            fp = v

        if isinstance(fp, (str, bytes, bytearray)):
            fdata = fp
        elif hasattr(fp, "read"):
            fdata = fp.read()
        elif fp is None:
            continue
        else:
            fdata = fp

        name = k.decode("utf-8") if isinstance(k, bytes) else k
        parts.append(
            _multipart_part(
                boundary_bytes, name, fdata, filename=fn, content_type=ft, extra_headers=fh
            )
        )

    body = b"".join(parts) + b"--%s--\r\n" % boundary_bytes
    content_type = f"multipart/form-data; boundary={boundary}"

    return body, content_type
