from geventhttpclient.header import Headers
from geventhttpclient.client import HTTPClient
from geventhttpclient.url import URL


class HTTPBinClient(HTTPClient):
    """Special HTTPClient with higher timeout values

    Args:
        HTTPClient (_type_): _description_
    """

    def __init__(
        self,
        host,
        port=None,
        headers=None,
        block_size=HTTPClient.BLOCK_SIZE,
        connection_timeout=30.0,
        network_timeout=30.0,
        disable_ipv6=True,
        concurrency=1,
        ssl=False,
        ssl_options=None,
        ssl_context_factory=None,
        insecure=False,
        proxy_host=None,
        proxy_port=None,
        version=HTTPClient.HTTP_11,
        headers_type=Headers,
    ):
        super().__init__(
            host,
            port=port,
            headers=headers,
            block_size=block_size,
            connection_timeout=connection_timeout,
            network_timeout=network_timeout,
            disable_ipv6=disable_ipv6,
            concurrency=concurrency,
            ssl=ssl,
            ssl_options=ssl_options,
            ssl_context_factory=ssl_context_factory,
            insecure=insecure,
            proxy_host=proxy_host,
            proxy_port=proxy_port,
            version=version,
            headers_type=headers_type,
        )
