import socket
import gevent
import ssl
import cookielib

from requests.adapters import HTTPAdapter, DEFAULT_POOLSIZE
from requests.exceptions import ConnectionError, SSLError, ProxyError
from requests.sessions import Session as _Session, merge_hooks, merge_setting
from requests.models import Request as _Request, PreparedRequest as _PreparedRequest, Response as _Response
from requests.cookies import cookiejar_from_dict, merge_cookies, RequestsCookieJar
from requests.auth import _basic_auth_str
from requests.utils import DEFAULT_CA_BUNDLE_PATH, get_encoding_from_headers, get_netrc_auth, to_native_string, default_headers
#                    prepend_scheme_if_needed, urldefragauth)

try:
    from requests.exceptions import ConnectTimeout
except ImportError:
    # Compatibility with previous versions
    from requests.exceptions import Timeout
    class ConnectTimeout(ConnectionError, Timeout):
        """The request timed out while trying to connect to the remote server.
    
        Requests that produced this error are safe to retry.
        """

from .client import HTTPClientPool
from .response import HTTPParseError
from .header import Headers
from .url import URL
# TODO: Add to_native_string on creation


DEFAULT_RETRIES = 5

class PreparedRequest(_PreparedRequest):
    # TODO: Fix this shit
    def prepare_headers(self, headers):
        """Prepares the given HTTP headers."""

        if headers:
            if isinstance(headers, Headers):
                self.headers = headers
            else:
                self.headers = Headers(headers)
        else:
            self.headers = Headers()


class Request(_Request):
    def prepare(self):
        """Constructs a :class:`PreparedRequest <PreparedRequest>` for transmission and returns it."""
        # Overwrite original PreparedRequest
        p = PreparedRequest()
        p.prepare(
            method=self.method,
            url=self.url,
            headers=self.headers,
            files=self.files,
            data=self.data,
            params=self.params,
            auth=self.auth,
            cookies=self.cookies,
            hooks=self.hooks,
        )
        return p


class Response(_Response):
    def __init__(self):
        self._content = False
        self._content_consumed = False
        self.status_code = None
        self.headers = None
        self.raw = None
        self.url = None
        self.encoding = None
        self.history = []
        self.reason = None
        self.cookies = cookiejar_from_dict({})
        self.elapsed = 0
        self.request = None

    def close(self):
        try:
            self.raw.release()
        except AttributeError:
            pass


class Session(_Session):
    def __init__(self):
        super(Session, self).__init__()
        self.headers = Headers(default_headers())
        self.adapters.clear()
        self.mount('http://', GHCAdapter())
        self.mount('https://', GHCAdapter(ssl=True))
        
    def prepare_request(self, request):
        """Constructs a :class:`PreparedRequest <PreparedRequest>` for
        transmission and returns it. The :class:`PreparedRequest` has settings
        merged from the :class:`Request <Request>` instance and those of the
        :class:`Session`.

        :param request: :class:`Request` instance to prepare with this
            session's settings.
        """
        cookies = request.cookies or {}

        # Bootstrap CookieJar.
        if not isinstance(cookies, cookielib.CookieJar):
            cookies = cookiejar_from_dict(cookies)

        # Merge with session cookies
        merged_cookies = merge_cookies(
            merge_cookies(RequestsCookieJar(), self.cookies), cookies)


        # Set environment's basic authentication if not explicitly set.
        auth = request.auth
        if self.trust_env and not auth and not self.auth:
            auth = get_netrc_auth(request.url)

        request.headers.update(self.headers)
        p = PreparedRequest()
        p.prepare(
            method=request.method.upper(),
            url=request.url,
            files=request.files,
            data=request.data,
            headers=request.headers,
            params=merge_setting(request.params, self.params),
            auth=merge_setting(auth, self.auth),
            cookies=merged_cookies,
            hooks=merge_hooks(request.hooks, self.hooks),
        )
        return p




class GHCAdapter(HTTPAdapter):
    def __init__(self, pool_connections=DEFAULT_POOLSIZE, max_retries=DEFAULT_RETRIES, 
                 timeout=None, **kwargs):
        # Make sure we got a simple number and not some strange thingy
        self.max_retries = int(max_retries)
        self.proxy_manager = {}

        if isinstance(timeout, tuple):
            try:
                connection_timeout, network_timeout = timeout
            except ValueError:
                # this may raise a string formatting error.
                err = ("Invalid timeout {0}. Pass a (connect, read) "
                       "timeout tuple, or a single float to set "
                       "both timeouts to the same value".format(timeout))
                raise ValueError(err)
        else:
            connection_timeout = network_timeout = timeout
        
        self.init_poolmanager(pool_connections, connection_timeout=connection_timeout, 
                              network_timeout=network_timeout, **kwargs)
        
    def init_poolmanager(self, connections, **pool_kwargs):
        self._pool_connections = connections
        self._pool_args = pool_kwargs
        self.poolmanager = HTTPClientPool(concurrency=connections, **pool_kwargs)

    def __getstate__(self):
        return dict((attr, getattr(self, attr, None)) for attr in
                    self.__attrs__)

    def __setstate__(self, state):
        # Can't handle by adding 'proxy_manager' to self.__attrs__ because
        # because self.poolmanager uses a lambda function, which isn't pickleable.
        self.proxy_manager = {}

        for attr, value in state.items():
            setattr(self, attr, value)

        self.init_poolmanager(self._pool_connections, **self._pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        raise NotImplementedError

    def cert_verify(self, conn, url, verify, cert):
        # SSL hassle is left to gevent
        raise NotImplementedError

    def build_response(self, req, resp):
        """Builds a :class:`Response <requests.Response>` object from a urllib3
        response. This should not be called from user code, and is only exposed
        for use when subclassing the
        :class:`HTTPAdapter <requests.adapters.HTTPAdapter>`

        :param req: The :class:`PreparedRequest <PreparedRequest>` used to generate the response.
        :param resp: The urllib3 response object.
        """
        response = Response()
        response.raw = resp

        response.headers = resp._headers_index
        response.status_code = response.raw.status_code
        response.reason = response.raw.status_message

        response.encoding = get_encoding_from_headers(response.headers)

        if isinstance(req.url, bytes):
            response.url = req.url.decode('utf-8')
        else:
            response.url = req.url

        # Add new cookies from the server.
        response.cookies.extract_cookies(resp, req)

        # Give the Response some context.
        response.request = req
        response.connection = self

        return response

    def _get_proxy(self, parsed_url, proxies):
        if not proxies:
            return None
        else:
            return proxies.get(parsed_url.scheme)

    def get_connection(self, parsed_url, proxies=None):
        proxy = self._get_proxy(parsed_url, proxies)

        if proxy:
            proxy_manager = self.proxy_manager_for(proxy)
            conn = proxy_manager.connection_from_url(parsed_url)
        else:
            conn = self.poolmanager.get_client(parsed_url)
        return conn

    def close(self):
        """Disposes of any internal state.

        Currently, this just closes the PoolManager, which closes pooled
        connections.
        """
        self.poolmanager.clear()

    def request_url(self, parsed_url, proxies):
        """Obtain the url to use when making the final request.

        If the message is being sent through a HTTP proxy, the full URL has to
        be used. Otherwise, we should only use the path portion of the URL.

        This should not be called from user code, and is only exposed for use
        when subclassing the
        :class:`HTTPAdapter <requests.adapters.HTTPAdapter>`.

        :param parsed_url: URL object of the request being sent.
        :param proxies: A dictionary of schemes to proxy URLs.
        """
        proxy = self._get_proxy(parsed_url, proxies)

        if proxy and parsed_url.scheme != 'https':
            url = parsed_url.stripped_auth()
        else:
            url = parsed_url
        return url

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        """Sends PreparedRequest object. Returns Response object.

        :param request: The :class:`PreparedRequest <PreparedRequest>` being sent.
        :param stream: (optional) Whether to stream the request content.
        :param timeout: (optional) Ignored right now. Set it for the whole adapter instead.
        :type timeout: float or tuple
        :param verify: (optional) Whether to verify SSL certificates.
        :param cert: (optional) Any user-provided SSL certificate to be trusted.
        :param proxies: (optional) The proxies dictionary to apply to the request.
        """

        parsed_url = URL(request.url)
        client = self.get_connection(parsed_url, proxies)
        parsed_url = self.request_url(parsed_url, proxies)

        try:
            resp = client.request(request.method, parsed_url.request_uri,
                                  body=request.body, headers=request.headers,
                                  max_retries=self.max_retries)
            # No low level handling of chunked requests. Client has to handle that
        except (HTTPParseError, socket.error) as err:
            raise ConnectionError(err, request=request)
        except (socket.timeout, gevent.Timeout) as e:
            raise ConnectTimeout(e, request=request)
        except ssl.SSLError as e:
            raise SSLError(e, request=request)
        
        return self.build_response(request, resp)
