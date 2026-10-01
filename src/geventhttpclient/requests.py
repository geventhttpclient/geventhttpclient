import json as jsonlib
import re
from codecs import getincrementaldecoder as _getincrementaldecoder
from collections.abc import Iterator
from http.cookiejar import CookieJar
from typing import Any, cast

from geventhttpclient import useragent
from geventhttpclient.header import HeadersDataType, parse_content_type_charset
from geventhttpclient.response import HTTPSocketResponse
from geventhttpclient.url import URL, ParamsDataType

# Split a Link header on commas that are not inside angle brackets. requests
# uses the same shortcut; it is good enough for the headers servers actually
# send, where link targets are always bracketed and relation attributes never
# contain commas of their own.
_LINK_HEADER_SPLIT = re.compile(r",\s*<")

# Default read chunk for ``iter_lines``; requests reads 512 at a time, we
# take a socket block worth of bytes.
_ITER_LINES_CHUNK_SIZE = 8192

# Line terminators for ``iter_lines``. HTTP allows \r\n, \r and \n; unlike
# bytes.splitlines we do not split on exotic control characters.
_LINE_TERMINATORS_BYTES = re.compile(rb"\r\n|\r|\n")
_LINE_TERMINATORS_STR = re.compile(r"\r\n|\r|\n")


def _parse_link_header(value: str) -> list[dict[str, str]]:
    """Parse a single Link header value into the list of links RFC 5988 describes."""
    out: list[dict[str, str]] = []
    if not value:
        return out
    for raw in _LINK_HEADER_SPLIT.split(value):
        try:
            url_part, params = raw.split(";", 1)
        except ValueError:
            url_part, params = raw, ""
        link: dict[str, str] = {"url": url_part.strip(" <>\"'")}
        for param in params.split(";"):
            if "=" not in param:
                continue
            key, val = param.split("=", 1)
            link[key.strip().lower()] = val.strip().strip("\"'")
        out.append(link)
    return out


class RequestsRequest(useragent.CompatRequest):
    @property
    def body(self) -> useragent.Payload:
        return self.payload


class RequestsResponse(useragent.CompatResponse):
    @property
    def request(self) -> useragent.CompatRequest | None:
        return self._request

    @property
    def ok(self) -> bool:
        return 100 <= self.status_code < 400

    def __bool__(self) -> bool:
        """A Response is truthy when its status code is below 400."""
        return self.ok

    @property
    def reason(self) -> str | None:
        return self._response.status_message

    @property
    def url(self) -> str:
        return self._request.url  # type: ignore[union-attr]

    @property
    def is_redirect(self) -> bool:
        """True if this Response is a well-formed HTTP redirect that
        :meth:`UserAgent.urlopen` would have followed.
        """
        return "location" in self.headers and self.status_code in useragent.REDIRECT_RESPONSE_CODES

    @property
    def is_permanent_redirect(self) -> bool:
        """True if this Response is one of the permanent redirect codes (301 or 308)."""
        return (
            "location" in self.headers
            and self.status_code in useragent.PERMANENT_REDIRECT_RESPONSE_CODES
        )

    @property
    def raw(self) -> HTTPSocketResponse:
        return self.stream

    def close(self) -> None:
        """Release the connection back to the pool. Alias for :meth:`release`."""
        self.release()

    @property
    def encoding(self) -> str | None:
        """The character set declared in the Content-Type header, if any.

        Mirrors the read side of ``requests.Response.encoding``; no automatic
        chardet / charset-normalizer fallback, since we have no such dep.
        """
        content_type = self.headers.get("content-type")
        if not content_type:
            return None
        # multiple Content-Type lines only happen for malformed responses,
        # use the first one and narrow away the list branch mypy would complain
        # about otherwise
        header = content_type if isinstance(content_type, str) else content_type[0]
        return parse_content_type_charset(header)

    @property
    def links(self) -> dict[str, dict[str, str]]:
        """The parsed ``Link`` header, keyed by the ``rel`` value of each link."""
        out: dict[str, dict[str, str]] = {}
        raw = self.headers.get("link")
        if not raw:
            return out
        # ``headers.get`` joins duplicates into a single str; in the unlikely
        # case of multiple Link header lines we still get a list back, narrow
        # it here so the per-header parser only ever sees a str
        headers_list: list[str]
        if isinstance(raw, list):
            headers_list = raw
        else:
            headers_list = [raw]
        for header in headers_list:
            for link in _parse_link_header(header):
                key = link.get("rel") or link.get("url")
                if key:
                    out[key] = link
        return out

    def raise_for_status(self) -> None:
        if 400 <= self.status_code < 600:
            raise useragent.BadStatusCode(self.url, code=self.status_code)

    def iter_content(
        self,
        chunk_size: int = 1,
        decode_unicode: bool = False,
    ) -> Iterator[bytes | str]:
        """Iterate over the body in chunks of ``chunk_size`` bytes.

        With ``decode_unicode=True`` chunks are decoded with the charset of
        the Content-Type header, falling back to utf-8. Matches the
        ``requests.Response.iter_content`` contract.
        """
        if decode_unicode:
            return self._iter_content_decoded(chunk_size)
        return self._iter_content_bytes(chunk_size)

    def _iter_content_bytes(self, chunk_size: int) -> Iterator[bytes]:
        chunk_size = max(int(chunk_size), 1)
        while True:
            chunk = self._response.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def _iter_content_decoded(self, chunk_size: int) -> Iterator[str]:
        """Decoded variant of ``iter_content``.

        An incremental decoder reassembles multibyte characters that straddle
        chunk boundaries, the way requests does; undecodable bytes are
        replaced.
        """
        chunk_size = max(int(chunk_size), 1)
        decoder = _getincrementaldecoder(self.encoding or "utf-8")(errors="replace")
        while True:
            chunk = self._response.read(chunk_size)
            if not chunk:
                return
            yield decoder.decode(chunk)

    def iter_lines(
        self,
        chunk_size: int = _ITER_LINES_CHUNK_SIZE,
        decode_unicode: bool = False,
        delimiter: bytes | None = None,
    ) -> Iterator[bytes | str]:
        """Iterate over the body split on ``delimiter``.

        Without a delimiter, lines terminate on \r\n, \r or \n. Lines that
        straddle chunk boundaries are reassembled; the last partial line,
        if any, is yielded at end-of-stream. The two modes (raw bytes and
        decoded str) split into helpers so each can keep its own clean types.
        """
        if decode_unicode:
            return self._iter_lines_decoded(chunk_size, delimiter)
        return self._iter_lines_bytes(chunk_size, delimiter)

    def _iter_lines_bytes(self, chunk_size: int, delimiter: bytes | None) -> Iterator[bytes]:
        pending = b""
        for chunk in self._iter_content_bytes(chunk_size):
            combined = pending + chunk
            hold = delimiter is None and combined.endswith(b"\r")
            if hold:
                # a trailing \r may be the first half of a \r\n pair that
                # straddles the chunk boundary; hold it back and decide in
                # the next round
                combined = combined[:-1]
            lines = (
                _LINE_TERMINATORS_BYTES.split(combined)
                if delimiter is None
                else combined.split(delimiter)
            )
            yield from lines[:-1]
            pending = (lines[-1] if lines else b"") + (b"\r" if hold else b"")
        if pending:
            # a held-back \r at end of stream terminates the pending line
            yield pending[:-1] if pending.endswith(b"\r") else pending

    def _iter_lines_decoded(self, chunk_size: int, delimiter: bytes | None) -> Iterator[str]:
        pending = ""
        for chunk in self._iter_content_decoded(chunk_size):
            combined = pending + chunk
            hold = delimiter is None and combined.endswith("\r")
            if hold:
                combined = combined[:-1]
            lines = (
                _LINE_TERMINATORS_STR.split(combined)
                if delimiter is None
                else combined.split(delimiter.decode("ascii"))
            )
            yield from lines[:-1]
            pending = (lines[-1] if lines else "") + ("\r" if hold else "")
        if pending:
            yield pending.removesuffix("\r")

    def json(self, **kw: Any) -> Any:
        """Decode the body as JSON.

        Like the parent :meth:`useragent.CompatResponse.json` but forwards
        ``**kw`` to ``json.loads`` (object_hook, parse_float, ...) and reads
        from the cached ``self.content`` rather than streaming through the
        socket response. ``json.loads`` auto-detects UTF-8/16/32, a charset
        declared in the Content-Type is not applied. Raises
        ``json.JSONDecodeError`` on bad input, which is already a
        ``ValueError``.
        """
        return jsonlib.loads(self.content, **kw)


class Session(useragent.UserAgent):
    """This class mimics and blatantly borrows with all due respect
    from the excellent and rightfully popular Requests API.

    Copyright of the original requests project:

    :copyright: (c) 2012 by Kenneth Reitz.
    :license: Apache2, see LICENSE for more details.
    """

    request_type = RequestsRequest
    response_type = RequestsResponse

    def get(self, url: str | URL, **kw: Any) -> RequestsResponse:
        r"""Sends a GET request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        kw.setdefault("allow_redirects", True)
        return self.request("GET", url, **kw)

    def options(self, url: str | URL, **kw: Any) -> RequestsResponse:
        r"""Sends a OPTIONS request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        kw.setdefault("allow_redirects", True)
        return self.request("OPTIONS", url, **kw)

    def head(self, url: str | URL, **kw: Any) -> RequestsResponse:
        r"""Sends a HEAD request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        kw.setdefault("allow_redirects", False)
        return self.request("HEAD", url, **kw)

    def post(
        self, url: str | URL, data: useragent.Payload = None, json: Any = None, **kw: Any
    ) -> RequestsResponse:
        r"""Sends a POST request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param data: (optional) Dictionary, list of tuples, bytes, or file-like
            object to send in the body of the HTTP Request.
        :param json: (optional) json to send in the body of the HTTP Request.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        return self.request("POST", url, data=data, json=json, **kw)

    def put(self, url: str | URL, data: useragent.Payload = None, **kw: Any) -> RequestsResponse:
        r"""Sends a PUT request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param data: (optional) Dictionary, list of tuples, bytes, or file-like
            object to send in the body of the HTTP Request.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        return self.request("PUT", url, data=data, **kw)

    def patch(self, url: str | URL, data: useragent.Payload = None, **kw: Any) -> RequestsResponse:
        r"""Sends a PATCH request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param data: (optional) Dictionary, list of tuples, bytes, or file-like
            object to send in the body of the HTTP Request.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        return self.request("PATCH", url, data=data, **kw)

    def delete(self, url: str | URL, **kw: Any) -> RequestsResponse:
        r"""Sends a DELETE request. Returns a HTTP Response object.

        :param url: URL for the new a HTTP Request object.
        :param \*\*kw: Optional arguments that ``request`` takes.
        :rtype: CompatResponse
        """

        return self.request("DELETE", url, **kw)

    def request(
        self,
        method: str,
        url: str | URL,
        params: ParamsDataType | None = None,
        data: useragent.Payload = None,
        headers: HeadersDataType | None = None,
        cookies: None = None,
        files: useragent.FilesInput | None = None,
        auth: None = None,
        timeout: float | tuple[float, float] | None = None,
        allow_redirects: bool = True,
        proxies: None = None,
        hooks: None = None,
        stream: bool | None = None,
        verify: bool | str | None = None,
        cert: str | tuple[str, str] | None = None,
        json: Any = None,
    ) -> RequestsResponse:
        """Constructs and sends a HTTP request, returns a HTTP Response object.

        NOTE: Only a subset of these parameters is currently (fully) supported.
        And it's also not

        :param method: method for the new request object.
        :param url: URL for the new request object.
        :param params: (optional) Dictionary or bytes to be sent in the query
            string for the request.
        :param data: (optional) Dictionary, list of tuples, bytes, or file-like
            object to send in the body of the request.
        :param json: (optional) json to send in the body of the
            HTTP Request.
        :param headers: (optional) Dictionary of HTTP Headers to send with the
            HTTP Request.
        :param cookies: (optional) Dict or CookieJar object to send with the
            HTTP Request.
        :param files: (optional) Dictionary of ``'filename': file-like-objects``
            for multipart encoding upload.
        :param auth: (optional) Auth tuple or callable to enable
            Basic/Digest/Custom HTTP Auth.
        :param timeout: (optional) How long to wait for the server to send
            data before giving up, as a float, or a :ref:`(connect timeout,
            read timeout) <timeouts>` tuple.
        :type timeout: float or tuple
        :param allow_redirects: (optional) Set to True by default.
        :type allow_redirects: bool
        :param proxies: (optional) Dictionary mapping protocol or protocol and
            hostname to the URL of the proxy.
        :param stream: (optional) whether to immediately download the response
            content. Defaults to ``False``.
        :param verify: (optional) Either a boolean, in which case it controls whether we verify
            the server's TLS certificate, or a string, in which case it must be a path
            to a CA bundle to use. Defaults to ``True``. When set to
            ``False``, requests will accept any TLS certificate presented by
            the server, and will ignore hostname mismatches and/or expired
            certificates, which will make your application vulnerable to
            man-in-the-middle (MitM) attacks. Setting verify to ``False``
            may be useful during local development or testing.
        :param cert: (optional) if String, path to ssl client cert file (.pem).
            If Tuple, ('cert', 'key') pair.
        :rtype: CompatResponse
        """
        # only ever used to report which keyword is unsupported, so the loops
        # below share one deliberately wide annotation
        param: Any
        for param in (timeout, cert, verify):
            if param is not None:
                raise ValueError(
                    f"{param} can not be set on a per-request basis. Please configure the UserAgent instead."
                )
        for param in (cookies, auth, proxies, hooks):
            if param is not None:
                raise NotImplementedError(
                    f"{param} is currently unsupported as a keyword argument."
                )
        for param in (hooks,):
            if param is not None:
                raise NotImplementedError(f"{param} is not supported")

        if json:
            if data:
                raise ValueError("Can send either data or json, not both at once")
            data = jsonlib.dumps(json)
            # work on a copy, the caller keeps their own headers
            headers = dict(headers) if headers else {}
            headers["Content-Type"] = "application/json"

        response = self.urlopen(
            url,
            method=method.upper(),
            headers=headers,
            files=files,
            payload=data or None,
            params=params,
            max_redirects=None if allow_redirects else 0,
        )
        if stream is False:
            # preload the data
            _ = response.content
        # to_string is False on every overload this surface accepts, so the
        # bytes overload of urlopen cannot reach us; response_type is declared
        # as type[CompatResponse] on the base class, but here it is
        # type[RequestsResponse].
        return cast(RequestsResponse, response)

    def __init__(self, *args: Any, **kw: Any) -> None:
        """
        requests.Session has no arguments at all. Unfortunately, we're relying way more
        on configuring the Session / UserAgent, while requests focuses more on
        configuring the single requests.
        """
        kw.setdefault("max_redirects", 30)
        super().__init__(*args, **kw)
        if not self.cookiejar:
            self.cookiejar = CookieJar()

    def _verify_status(self, status_code: int, url: str | URL | None = None) -> None:
        # Don't raise, whatever the status is
        pass
