import errno
import os
import socket
import ssl
import sys
import zlib
from urllib.parse import urlencode

import brotli
import gevent
from urllib3 import encode_multipart_formdata
from urllib3.fields import RequestField

from geventhttpclient.client import HTTPClient, HTTPClientPool
from geventhttpclient.url import URL, to_key_val_list


class ConnectionError(Exception):
    def __init__(self, url, *args, **kwargs):
        self.url = url
        self.__dict__.update(kwargs)
        if args and isinstance(args[0], str):
            self.text = args[0] + ": " + str(args[1:])
        else:
            self.text = str(args[0]) if len(args) == 1 else ""
        if kwargs:
            self.text += ", " if self.text else ""
            self.kwargs_text = ", ".join(f"{key}={val}" for key, val in kwargs.items())
            self.text += self.kwargs_text
        else:
            self.text = ""

    def __str__(self):
        if self.text:
            return f"URL {self.url}: {self.text}"
        else:
            return f"URL {self.url}"

    def __repr__(self):
        repr_str = super().__repr__()
        if self.kwargs_text:
            return repr_str.replace(")", "".join([", ", self.kwargs_text, ")"]))
        return repr_str


class RetriesExceeded(ConnectionError):
    pass


class BadStatusCode(ConnectionError):
    pass


class EmptyResponse(ConnectionError):
    pass


class CompatRequest:
    """urllib / cookielib compatible request class.
    See also: http://docs.python.org/library/cookielib.html
    """

    def __init__(self, url, method="GET", headers=None, payload=None, params=None):
        self.params = params
        self.set_url(url)
        self.original_host = self.url_split.host
        self.method = method
        self.headers = headers
        self.payload = payload

    def set_url(self, url):
        if isinstance(url, URL):
            self.url = str(url)
            self.url_split = url
        else:
            self.url = url
            self.url_split = URL(self.url, params=self.params)

    def get_full_url(self):
        return self.url

    def get_host(self):
        return self.url_split.host

    def get_type(self):
        return self.url_split.scheme

    def get_origin_req_host(self):
        return self.original_host

    def is_unverifiable(self):
        """See http://tools.ietf.org/html/rfc2965.html. Not fully implemented!"""
        return False

    @property
    def unverifiable(self):
        return self.is_unverifiable()

    def get_header(self, header_name, default=None):
        return self.headers.get(header_name, default)

    def has_header(self, header_name):
        return header_name in self.headers

    def header_items(self):
        return list(self.headers.items())

    def add_unredirected_header(self, key, val):
        self.headers.add(key, val)

    def _drop_payload(self):
        self.method = "GET"
        self.payload = None
        for item in ("content-length", "content-type", "content-encoding"):
            self.headers.discard(item)

    def _drop_cookies(self):
        for item in ("cookie", "cookie2"):
            self.headers.discard(item)

    def redirect(self, code, location):
        """Modify the request inplace to point to the new location"""
        self.set_url(self.url_split.redirect(location))
        if code in (302, 303):
            self._drop_payload()
        self._drop_cookies()


class CompatResponse:
    """Adapter for urllib responses with some extensions"""

    __slots__ = "headers", "_response", "_request", "_sent_request", "_cached_content"

    def __init__(self, ghc_response, request=None, sent_request=None):
        self._response = ghc_response
        self._request = request
        self._sent_request = sent_request
        self.headers = self._response._headers_index

    @property
    def status(self):
        """The returned http status"""
        # TODO: Should be a readable string
        return str(self.status_code)

    @property
    def status_code(self):
        """The http status code as plain integer"""
        return self._response.get_code()

    @property
    def stream(self):
        return self._response

    def read(self, n=None):
        """Read n bytes from the response body"""
        return self._response.read(n)

    def readline(self):
        return self._response.readline()

    def release(self):
        return self._response.release()

    def unzipped(self, gzip=True, br=False):
        bodystr = self._response.read()
        if gzip:
            return zlib.decompress(bodystr, 16 + zlib.MAX_WBITS)
        elif br:
            return brotli.decompress(bodystr)
        else:
            # zlib only provides the zlib compress format, not the deflate format;
            # so on top of all there's this workaround:
            try:
                return zlib.decompress(bodystr, -zlib.MAX_WBITS)
            except zlib.error:
                return zlib.decompress(bodystr)

    @property
    def content(self):
        """Unzips if necessary and buffers the received body. Careful with large files!"""
        try:
            return self._cached_content
        except AttributeError:
            self._cached_content = self._content()
            return self._cached_content

    def _content(self):
        try:
            content_type = self.headers.getheaders("content-encoding")[0].lower()
        except IndexError:
            # No content-encoding header set
            content_type = "identity"

        if content_type == "gzip":
            ret = self.unzipped(gzip=True)
        elif content_type == "deflate":
            ret = self.unzipped(gzip=False)
        elif content_type == "identity":
            ret = self._response.read()
        elif content_type == "br":
            ret = self.unzipped(gzip=False, br=True)
        elif content_type == "compress":
            raise ValueError(f"Compression type not supported: {content_type}")
        else:
            raise ValueError(f"Unknown content encoding: {content_type}")

        self.release()
        return ret

    def __len__(self):
        """The content lengths as should be returned from the headers"""
        try:
            return int(self.headers.getheaders("content-length")[0])
        except (IndexError, ValueError):
            return len(self.content)

    def __nonzero__(self):
        """If we have an empty response body, we still don't want to evaluate as false"""
        return True

    def info(self):
        """Adaption to cookielib: Alias for headers"""
        return self.headers

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()


class UserAgent:
    response_type = CompatResponse
    request_type = CompatRequest
    valid_response_codes = frozenset([200, 206, 301, 302, 303, 307])
    redirect_resonse_codes = frozenset([301, 302, 303, 307])

    def __init__(
        self,
        max_redirects=3,
        max_retries=3,
        retry_delay=0,
        cookiejar=None,
        headers=None,
        **kwargs,
    ):
        self.max_redirects = int(max_redirects)
        self.max_retries = int(max_retries)
        self.retry_delay = retry_delay
        self.default_headers = HTTPClient.DEFAULT_HEADERS.copy()
        if headers:
            self.default_headers.update(headers)
        self.cookiejar = cookiejar
        self.clientpool = HTTPClientPool(**kwargs)

    def close(self):
        self.clientpool.close()

    def __del__(self):
        self.close()

    def _make_request(self, url, method="GET", headers=None, payload=None, params=None, files=None):
        req_headers = self.default_headers.copy()
        if headers:
            req_headers.update(headers)
        if payload or files:
            # Adjust headers depending on payload content
            content_type = req_headers.get("content-type", None)
            if files:
                (body, content_type) = self._encode_files(files, payload)
                payload = body
                req_headers["content-type"] = content_type
            if isinstance(payload, dict):
                if not content_type:
                    req_headers["content-type"] = "application/x-www-form-urlencoded; charset=utf-8"
                payload = urlencode(payload)
            elif not content_type and isinstance(payload, str):
                req_headers["content-type"] = "text/plain; charset=utf-8"
            elif not content_type:
                req_headers["content-type"] = "application/octet-stream"
        return self.request_type(
            url, method=method, headers=req_headers, payload=payload, params=params
        )

    def _urlopen(self, request):
        client = self.clientpool.get_client(request.url_split)
        resp = client.request(
            request.method,
            request.url_split.request_uri,
            body=request.payload,
            headers=request.headers,
        )
        return self.response_type(resp, request=request, sent_request=resp._sent_request)

    def _verify_status(self, status_code, url=None):
        """Hook for subclassing"""
        if status_code not in self.valid_response_codes:
            raise BadStatusCode(url, code=status_code)

    def _encode_files(self, files, data):
        """
        Method taken from models in requests library , usage is the same. Only difference is that you can add custom
        boundary in 5-tuple version.

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

        new_fields = []
        fields = to_key_val_list(data or {})
        files = to_key_val_list(files or {})

        for field, val in fields:
            if isinstance(val, (str, bytes)) or not hasattr(val, "__iter__"):
                val = [val]
            for v in val:
                if v is not None:
                    if not isinstance(v, bytes):
                        v = str(v)

                    new_fields.append(
                        (
                            field.decode("utf-8") if isinstance(field, bytes) else field,
                            v.encode("utf-8") if isinstance(v, str) else v,
                        )
                    )

        for k, v in files:
            # support for explicit filename
            ft = None
            fh = None
            boundary = None
            if isinstance(v, (tuple, list)):
                if len(v) == 2:
                    fn, fp = v
                elif len(v) == 3:
                    fn, fp, ft = v
                elif len(v) == 4:
                    fn, fp, ft, fh = v
                else:
                    fn, fp, ft, fh, boundary = v
            else:
                fn = self.guess_filename(v) or k
                fp = v

            if isinstance(fp, (str, bytes, bytearray)):
                fdata = fp
            elif hasattr(fp, "read"):
                fdata = fp.read()
            elif fp is None:
                continue
            else:
                fdata = fp

            rf = RequestField(name=k, data=fdata, filename=fn, headers=fh)
            rf.make_multipart(content_type=ft)
            new_fields.append(rf)

        body, content_type = encode_multipart_formdata(new_fields, boundary)

        return body, content_type

    def _handle_error(self, e, url=None):
        """Hook for subclassing. Raise the error to interrupt further retrying,
        return it to continue retries and save the error, when retries
        exceed the limit.
        Temporary errors should be swallowed here for automatic retries.
        """
        if isinstance(e, (socket.timeout, gevent.Timeout)):
            return e
        elif isinstance(e, socket.error) and e.errno in {
            errno.ETIMEDOUT,
            errno.ENOLINK,
            errno.ENOENT,
            errno.EPIPE,
        }:
            return e
        elif isinstance(e, ssl.SSLError) and "read operation timed out" in str(e):
            return e
        elif isinstance(e, EmptyResponse):
            return e
        raise e.with_traceback(sys.exc_info()[2])

    def _handle_retries_exceeded(self, url, last_error=None):
        """Hook for subclassing"""
        raise RetriesExceeded(url, self.max_retries, original=last_error)

    def urlopen(
        self,
        url,
        method="GET",
        response_codes=valid_response_codes,
        headers=None,
        payload=None,
        to_string=False,
        debug_stream=None,
        params=None,
        **kwargs,
    ):
        """Open an URL, do retries and redirects and verify the status code"""
        # POST or GET parameters can be passed in **kwargs
        if kwargs:
            if not payload:
                payload = kwargs
            elif isinstance(payload, dict):
                payload.update(kwargs)
            files = kwargs.get("files", None)
        else:
            files = None
        req = self._make_request(
            url,
            method=method,
            headers=headers,
            payload=payload,
            params=params,
            files=files,
        )
        for retry in range(self.max_retries + 1):
            if retry > 0 and self.retry_delay:
                # Don't wait the first time and skip if no delay specified
                gevent.sleep(self.retry_delay)
            for _ in range(self.max_redirects + 1):
                if self.cookiejar is not None:
                    self.cookiejar.add_cookie_header(req)

                try:
                    resp = self._urlopen(req)
                except gevent.GreenletExit:
                    raise
                except BaseException as e:
                    e.request = req
                    last_error = self._handle_error(e, url=req.url)
                    break  # Continue with next retry

                # We received a response
                if debug_stream is not None:
                    debug_stream.write(
                        self._conversation_str(req.url, resp, payload=req.payload) + "\n\n"
                    )

                if self.cookiejar is not None:
                    self.cookiejar.extract_cookies(resp, req)

                try:
                    self._verify_status(resp.status_code, url=req.url)
                except Exception as e:
                    # Basic transmission successful, but not the wished result
                    # Let's collect some debug info
                    e.response = resp
                    e.request = req
                    e.http_log = self._conversation_str(req.url, resp, payload=req.payload)
                    resp.release()
                    last_error = self._handle_error(e, url=req.url)
                    break  # Continue with next retry

                redirection = resp.headers.get("location")
                if isinstance(redirection, bytes):
                    redirection = redirection.decode("utf-8")
                if resp.status_code in self.redirect_resonse_codes and redirection:
                    resp.release()
                    try:
                        req.redirect(resp.status_code, redirection)
                        continue
                    except Exception as e:
                        last_error = self._handle_error(e, url=req.url)
                        break

                if not to_string:
                    return resp
                else:
                    # to_string added as parameter, to handle empty response
                    # bodies as error and continue retries automatically
                    try:
                        ret = resp.content
                    except Exception as e:
                        last_error = self._handle_error(e, url=req.url)
                        break
                    else:
                        if not ret:
                            e = EmptyResponse(url, "Empty response body received")
                            last_error = self._handle_error(e, url=req.url)
                            break
                        else:
                            return ret
            else:
                e = RetriesExceeded(url, f"Redirection limit reached ({self.max_redirects})")
                last_error = self._handle_error(e, url=url)
        else:
            return self._handle_retries_exceeded(url, last_error=last_error)

    @classmethod
    def _conversation_str(cls, url, resp, payload=None):
        header_str = "\n".join(f"{key}: {val}" for key, val in resp.headers.items())
        ret = "REQUEST: " + url + "\n" + resp._sent_request
        if payload:
            if isinstance(payload, bytes):
                try:
                    ret += payload.decode("utf-8") + "\n\n"
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
            + resp.content[:].decode("utf-8")
        )
        return ret

    @classmethod
    def guess_filename(cls, file):
        """Tries to guess the filename of the given object."""
        name = getattr(file, "name", None)
        if name and isinstance(name, (str, bytes)) and name[0] != "<" and name[-1] != ">":
            return os.path.basename(name)

    def download(self, url, fpath, chunk_size=16 * 1024, resume=False, **kwargs):
        kwargs.pop("to_string", None)
        headers = kwargs.pop("headers", {})
        headers["Connection"] = "Keep-Alive"
        if resume and os.path.isfile(fpath):
            offset = os.path.getsize(fpath)
        else:
            offset = 0

        for _ in range(self.max_retries + 1):
            if offset:
                headers["Range"] = f"bytes={offset}-"
                resp = self.urlopen(url, headers=headers, **kwargs)
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
                headers.pop("Range", None)
                resp = self.urlopen(url, headers=headers, **kwargs)

            with open(fpath, "ab" if offset else "wb") as f:
                if offset:
                    f.seek(offset, os.SEEK_SET)
                try:
                    data = resp.read(chunk_size)
                    with resp:
                        while data:
                            f.write(data)
                            data = resp.read(chunk_size)
                except BaseException as e:
                    self._handle_error(e, url=url)
                    if resp.headers.get("accept-ranges") == "bytes":
                        # Only if this header is set, we can fall back to partial download
                        offset = f.tell()
                    continue
            # All done, break outer loop
            break
        else:
            self._handle_retries_exceeded(url, last_error=e)
        return resp
