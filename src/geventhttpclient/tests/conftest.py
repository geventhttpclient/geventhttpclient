import gevent.monkey

gevent.monkey.patch_all()  # make sure all tests run monkey patched

import geventhttpclient.httplib

geventhttpclient.httplib.patch()  # required for the httplib2 tests
