"""Authentication helpers shared by the requests and httpx interfaces.

``BasicAuth`` is a first-class authentication object: instantiate it
with a username and password, pass it to a session or a single
request, and it sets the ``Authorization`` header.
"""

import base64


class BasicAuth:
    """HTTP Basic authentication: ``BasicAuth("user", "pass")``.

    The credentials are concatenated with a colon and base64-encoded into
    the ``Authorization`` header. The encoding follows RFC 7617: the
    default is *auto-detect*, which tries ISO-8859-1 (latin-1) first
    and falls back to UTF-8 when the username or password contains a
    code point that latin-1 cannot represent. Pass ``encoding=...`` to
    pick a specific codec instead.
    """

    def __init__(self, username: str, password: str, *, encoding: str | None = None) -> None:
        self._username = str(username)
        self._password = str(password)
        raw = f"{self._username}:{self._password}"
        if encoding is None:
            # RFC 7617: try the classic ISO-8859-1 encoding first (RFC 7616
            # "Basic"); if the credentials contain a code point outside the
            # latin-1 range, fall back to UTF-8 (RFC 7617 "Basic", charset
            # attribute). The probe uses strict errors so it raises rather
            # than silently substituting unknown code points.
            try:
                self._credentials = raw.encode("iso-8859-1", errors="strict")
                self._encoding = "iso-8859-1"
            except UnicodeEncodeError:
                self._credentials = raw.encode("utf-8")
                self._encoding = "utf-8"
        else:
            self._credentials = raw.encode(encoding)
            self._encoding = encoding
        self.token = b"Basic " + base64.b64encode(self._credentials)

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    @property
    def encoding(self) -> str:
        """The codec used to turn ``username:password`` into bytes:
        ``"iso-8859-1"`` or ``"utf-8"`` under auto-detect, or the
        caller-chosen value when ``encoding=`` was passed."""
        return self._encoding

    def auth_header(self) -> str:
        """The value to put in the ``Authorization`` request header."""
        return self.token.decode("latin-1")

    def __repr__(self) -> str:
        return f"BasicAuth(username={self._username!r}, encoding={self._encoding!r})"


# Anything that resolve_auth() knows how to turn into a header value.
AuthValue = BasicAuth | tuple[str, str] | str | None


def resolve_auth(auth: AuthValue) -> str | None:
    """Normalize the ``auth`` parameter into an ``Authorization`` header value.

    Accepts ``None``, a ``BasicAuth`` instance, a 2-tuple of
    ``(username, password)``, or an already-built header value (any
    string). Anything else raises ``NotImplementedError`` so callers
    can surface a clear error.
    """
    if auth is None:
        return None
    if isinstance(auth, BasicAuth):
        return auth.auth_header()
    if isinstance(auth, str):
        return auth
    if isinstance(auth, tuple) and len(auth) == 2:
        return BasicAuth(*auth).auth_header()
    raise NotImplementedError(
        f"Unsupported auth type: {type(auth).__name__}. "
        "Use BasicAuth or a (username, password) tuple."
    )


__all__ = ["AuthValue", "BasicAuth", "resolve_auth"]
