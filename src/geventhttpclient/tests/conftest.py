import gevent.monkey

gevent.monkey.patch_all()  # make sure all tests run monkey patched
