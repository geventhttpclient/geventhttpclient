# package

__version__ = "2.3.0"  # dont forget to update version in pyproject.toml as well

from geventhttpclient.api import delete, get, head, options, patch, post, put, request
from geventhttpclient.client import HTTPClient
from geventhttpclient.requests import Session
from geventhttpclient.url import URL
from geventhttpclient.useragent import UserAgent
