if __name__ == "__main__":
    from gevent import monkey

    monkey.patch_all()

    import time

    import gevent.pool
    from restkit import *
    from socketpool import ConnectionPool

    url = "http://127.0.0.1/index.html"

    N = 1000
    C = 10

    Pool = ConnectionPool(factory=Connection, backend="gevent", max_size=C, timeout=300)

    def run():
        response = request(url, follow_redirect=True, pool=Pool)
        response.body_string()
        assert response.status_int == 200

    group = gevent.pool.Pool(size=C)

    for i in range(5):
        now = time.time()
        for _ in range(N):
            group.spawn(run)
        group.join()

        delta = time.time() - now
        req_per_sec = N / delta

        print(f"request count:{N}, concurrenry:{C}, {req_per_sec} req/s")
