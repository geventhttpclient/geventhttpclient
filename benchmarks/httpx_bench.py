import time

import gevent.pool
import httpx

# For httpx in its native concurrency model, see httpx_async_bench.py.


def main(n=1000, concurrency=10, url="http://127.0.0.1/"):
    def run(client):
        response = client.get(url)
        assert response.status_code == 200

    client = httpx.Client(
        limits=httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=concurrency,
        )
    )
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
