import time

import gevent.pool
import urllib3


def main(n=1000, concurrency=10, url="http://127.0.0.1/"):
    def run(client):
        response = client.request("GET", url)
        assert response.status == 200

    client = urllib3.PoolManager()
    group = gevent.pool.Pool(size=concurrency)

    for i in range(5):
        now = time.time()
        for _ in range(n):
            group.spawn(run, client)
        group.join()

        delta = time.time() - now
        req_per_sec = n / delta

        print(f"request count:{n}, concurrency:{concurrency}, {req_per_sec:.2f} req/s")


if __name__ == "__main__":
    # main(n=10, concurrency=5, url="https://github.com")
    main()
