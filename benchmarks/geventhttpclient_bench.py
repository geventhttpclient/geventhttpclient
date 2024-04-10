import time

import gevent.pool

from geventhttpclient import URL, HTTPClient


def main(n=1000, concurrency=10, url="http://127.0.0.1/"):
    url = URL(url)
    request_uri = url.request_uri

    def run(client):
        response = client.get(request_uri)
        content = response.read()
        assert content
        assert response.status_code == 200

    client = HTTPClient.from_url(url, concurrency=concurrency)
    group = gevent.pool.Pool(size=concurrency)
    run(client)

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
