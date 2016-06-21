import sys
import pytest

import gevent
import gevent.ssl

class DisableSSL(object):
    def __enter__(self):
        self._modules = dict()
        # pretend there is no ssl support
        self._modules['ssl'] = sys.modules.pop('ssl', None)
        sys.modules['ssl'] = None

        # ensure gevent must be re-imported to fire an ssl ImportError
        for module_name in [k for k in sys.modules.keys() if k.startswith('gevent')]:
            self._modules[module_name] = sys.modules.pop(module_name)

    def __exit__(self, *args, **kwargs):
        # Restore all previously disabled modules
        sys.modules.update(self._modules)


def test_import_with_nossl():
    with DisableSSL():
        from geventhttpclient import httplib
        from geventhttpclient import HTTPClient

def test_httpclient_raises_with_no_ssl():
    with DisableSSL():
        from geventhttpclient import HTTPClient
        with pytest.raises(Exception):
            HTTPClient.from_url("https://httpbin.org/")


if __name__ == '__main__':
    test_import_with_nossl()
    test_httpclient_raises_with_no_ssl()
