"""HTTP/2 transport error hierarchy.

Review_http2_3.md H3 noted that h2 transport failures surfaced as
raw :class:`RuntimeError` / :class:`HTTP2ConnectionPoolError` / bare
:class:`TimeoutError`, breaking the ``ConnectionError`` contract that
locust-style callers expect. This module exposes a single
``HTTP2Error(ConnectionError)`` that every h2-specific failure mode
is mapped into, so ``except ConnectionError`` (or the existing
``UserAgent._handle_error`` retry loop) catches h2 the same way it
catches h1.

The class lives here (rather than in ``useragent.py``) so the lower
layers can import it without circular dependencies.
"""

from typing import Any


class HTTP2Error(ConnectionError):
    """A failure during an HTTP/2 round-trip.

    Carries the optional ``response`` / ``request`` that was active
    at the time the error surfaced, mirroring :class:`BadStatusCode`.
    """

    def __init__(
        self,
        message: str,
        *,
        response: Any = None,
        request: Any = None,
    ) -> None:
        super().__init__(message)
        self.response = response
        self.request = request


__all__ = ["HTTP2Error"]
