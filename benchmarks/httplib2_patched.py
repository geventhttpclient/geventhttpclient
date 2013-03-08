if __name__ == "__main__":

    from geventhttpclient import httplib
    httplib.patch()

    import httplib2
    import time
    import gevent.queue
    import gevent.pool
    from contextlib import contextmanager

    class ConnectionPool(object):

        def __init__(self, factory, size=5):
            self.factory = factory
            self.queue = gevent.queue.Queue(size)
            for i in xrange(size):
                self.queue.put(factory())

        @contextmanager
        def use(self):
            el = self.queue.get()
            yield el
            self.queue.put(el)


    def httplib2_factory(*args, **kw):
        def factory():
            return httplib2.Http(*args, **kw)
        return factory


    N = 1000
    C = 10

    url = 'http://127.0.0.1/index.html'

    def run(pool):
        with pool.use() as http:
            http.request(url)


    http_pool = ConnectionPool(httplib2_factory(), size=C)
    group = gevent.pool.Pool(size=C)

    for i in xrange(5):
        now = time.time()
        for _ in xrange(N):
            group.spawn(run, http_pool)
        group.join()
    
        delta = time.time() - now
        req_per_sec = N / delta
    
        print "request count:%d, concurrenry:%d, %f req/s" % (
            N, C, req_per_sec)


