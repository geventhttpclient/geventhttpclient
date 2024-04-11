"""
Drop-in replacement for httplib2. Make sure to monkey patch everything first!

import gevent.monkey
gevent.monkey.patch_all()

from geventhttpclient import httplib
httplib.patch()

from geventhttpclient import httplib2

http = httplib2.Http(concurrency=5)
"""

from httplib2 import Http as httplib2_Http

from geventhttpclient.connectionpool import ClientPool


class Http:
    """Imitate a httplib2.Http client. Except that it is now run concurrently."""

    def __init__(self, *args, **kw):
        self.concurrency = max(kw.pop("concurrency", 0), 2)  # never smaller than 2
        self.args = args
        self.kw = kw
        self.pool = ClientPool(self._http_factory, concurrency=self.concurrency)

    def _http_factory(self):
        return httplib2_Http(*self.args, **self.kw)

    def request(self, *args, **kw):
        with self.pool.get() as client:
            return client.request(*args, **kw)

    def close(self):
        self.pool.close()
