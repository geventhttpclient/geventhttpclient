if __name__ == "__main__":

    from gevent import monkey
    monkey.patch_all()

    import gevent.pool
    import time

    from restkit import request
    from restkit.globals import set_manager
    from restkit.manager.mgevent import GeventManager

    url = 'http://127.0.0.1/index.html'

    N = 1000
    C = 10

    manager = GeventManager(timeout=300, max_conn=C, reap_connections=True)
    set_manager(manager)


    def run():
        response = request(url)
        response.body_string()
        assert response.status_int == 200


    group = gevent.pool.Pool(size=C)

    now = time.time()
    for _ in xrange(N):
        group.spawn(run)
    group.join()

    delta = time.time() - now
    req_per_sec = N / delta

    print "request count:%d, concurrenry:%d, %f req/s" % (
        N, C, req_per_sec)


