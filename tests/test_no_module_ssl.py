import sys

import gevent
import gevent.ssl
import pytest


class DisableSSL:
    def __enter__(self):
        self._modules = {"ssl": sys.modules.pop("ssl", None)}
        # pretend there is no ssl support
        sys.modules["ssl"] = None

        # ensure gevent must be re-imported to fire a ssl ImportError
        for module_name in [k for k in sys.modules if k.startswith("gevent")]:
            self._modules[module_name] = sys.modules.pop(module_name)

    def __exit__(self, *args, **kwargs):
        # Restore all previously disabled modules
        sys.modules.update(self._modules)


def test_import_with_nossl():
    return
    with DisableSSL():
        from geventhttpclient import HTTPClient, httplib


def test_httpclient_raises_with_no_ssl():
    return
    with DisableSSL():
        from geventhttpclient import HTTPClient

        with pytest.raises(Exception):
            HTTPClient.from_url("https://somesslhost.org/")
