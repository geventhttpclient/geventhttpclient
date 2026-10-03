"""HTTP/2 (RFC 9113) client support.

Public API:

* :class:`HTTP2Error` -- uniform failure type for every h2 transport
  failure (``ConnectionError`` subclass). Callers can
  ``except ConnectionError`` and catch h1 + h2 uniformly.
* :class:`HTTP2ConnectionPool` and :class:`HTTP2ConnectionPoolError`
  -- multiplexed session manager.
* :class:`HTTP2Session`, :class:`HTTP2ResponseHandle`,
  :class:`HTTP2Wire`, :class:`HTTP2WireError` -- sans-IO pump plus
  blocking handle wrappers.
* :class:`HTTP2Response`, :class:`HTTP2ResponseError`,
  :class:`HTTP2SocketResponseBridge` -- response wrapper used by
  :mod:`geventhttpclient.useragent`.
* Sans-IO protocol building blocks (events, dataclasses, constants):
  :data:`DEFAULT_MAX_CONCURRENT_STREAMS`,
  :class:`HTTP2Connection`, :class:`Http2Event`,
  :class:`StreamLifecycle`, :class:`StreamState`,
  :class:`HeadersReceived`, :class:`DataReceived`,
  :class:`StreamReset`, :class:`StreamClosed`,
  :class:`SettingsReceived`, :class:`PingReceived`,
  :class:`GoAwayReceived`, :class:`WindowUpdateReceived`,
  :class:`InformationalResponseReceived`,
  :class:`TrailerReceived`.

The C extension (``geventhttpclient.http2._parser``) and the
``_core`` module are considered private and may change without
notice.
"""

from geventhttpclient.http2._core import (
    CONNECTION_STREAM_ID,
    DEFAULT_MAX_CONCURRENT_STREAMS,
    DataReceived,
    GoAwayReceived,
    HeadersReceived,
    HTTP2Connection,
    Http2Event,
    InformationalResponseReceived,
    PingReceived,
    SettingsReceived,
    StreamClosed,
    StreamLifecycle,
    StreamReset,
    StreamState,
    TrailerReceived,
    WindowUpdateReceived,
)
from geventhttpclient.http2.errors import HTTP2Error
from geventhttpclient.http2.pool import HTTP2ConnectionPool, HTTP2ConnectionPoolError
from geventhttpclient.http2.response import (
    HTTP2Response,
    HTTP2ResponseError,
    HTTP2SocketResponseBridge,
)
from geventhttpclient.http2.session import (
    HTTP2ResponseHandle,
    HTTP2Session,
    HTTP2Wire,
    HTTP2WireError,
)

__all__ = [
    "CONNECTION_STREAM_ID",
    "DEFAULT_MAX_CONCURRENT_STREAMS",
    "DataReceived",
    "GoAwayReceived",
    "HTTP2Connection",
    "HTTP2ConnectionPool",
    "HTTP2ConnectionPoolError",
    "HTTP2Error",
    "HTTP2Response",
    "HTTP2ResponseError",
    "HTTP2ResponseHandle",
    "HTTP2Session",
    "HTTP2SocketResponseBridge",
    "HTTP2Wire",
    "HTTP2WireError",
    "HeadersReceived",
    "Http2Event",
    "InformationalResponseReceived",
    "PingReceived",
    "SettingsReceived",
    "StreamClosed",
    "StreamLifecycle",
    "StreamReset",
    "StreamState",
    "TrailerReceived",
    "WindowUpdateReceived",
]
