import sys
from unittest import TestCase


def delete_item(obj, item):
    try:
        del obj[item]
    except KeyError:
        pass


class DisableSSL(object):
    def __enter__(self):
        # pretend there is no ssl support
        sys.modules["ssl"] = None

        # ensure gevent must be re-imported to fire an ssl ImportError
        delete_item(sys.modules, "gevent")
        delete_item(sys.modules, "gevent.ssl")
        delete_item(sys.modules, "geventhttpclient")
        delete_item(sys.modules, "geventhttpclient.response")
        delete_item(sys.modules, "geventhttpclient.httplib")

    def __exit__(self, *args, **kwargs):
        if 'ssl' in sys.modules:
            del sys.modules["ssl"]


class TestNoModuleSSL(TestCase):
    def run(self, *args, **kwargs):
        with DisableSSL():
            super(TestNoModuleSSL, self).run(*args, **kwargs)

    def test_import_with_nossl(self):
        from geventhttpclient import httplib
        from geventhttpclient import HTTPClient

    def test_httpclient_raises_with_no_ssl(self):
        from geventhttpclient import HTTPClient
        with self.assertRaises(Exception):
            HTTPClient.from_url("https://httpbin.org/")
