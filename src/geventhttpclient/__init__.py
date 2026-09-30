# package

# The version is derived from git tags by setuptools-scm. The version file
# is generated at build time; never edit or commit it.
try:
    from geventhttpclient._version import __version__
except ImportError:  # source tree without a build step
    __version__ = "0.0.0+unknown"
from geventhttpclient.api import delete, get, head, options, patch, post, put, request
from geventhttpclient.client import HTTPClient
from geventhttpclient.requests import Session
from geventhttpclient.url import URL
from geventhttpclient.useragent import UserAgent
