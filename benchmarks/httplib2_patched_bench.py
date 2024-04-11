import gevent.monkey

gevent.monkey.patch_all()

from geventhttpclient import httplib

httplib.patch()

import time

import gevent.pool
import gevent.queue

from geventhttpclient import httplib2


def main(n=1000, concurrency=10, url="http://127.0.0.1/"):
    def run(http):
        response, content = http.request(url)
        assert content
        assert b"body" in content

    http_pool = httplib2.Http(concurrency=concurrency)
    group = gevent.pool.Pool(size=concurrency)

    for i in range(5):
        now = time.time()
        for _ in range(n):
            group.spawn(run, http_pool)
        group.join()

        delta = time.time() - now
        req_per_sec = n / delta

        print(f"request count:{n}, concurrency:{concurrency}, {req_per_sec:.2f} req/s")


if __name__ == "__main__":
    # main(n=10, concurrency=5, url="https://github.com")
    main()
