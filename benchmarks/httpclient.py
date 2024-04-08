import time

import gevent.pool

from geventhttpclient import URL, HTTPClient

if __name__ == "__main__":
    N = 1000
    C = 10

    url = URL("http://127.0.0.1/index.html")
    qs = url.request_uri

    def run(client):
        response = client.get(qs)
        response.read()
        assert response.status_code == 200

    # For better compatibility, especially with cookies, use headers_type=Headers
    # The difference is 2900 requests/s with dict vs 2450 with Headers on my machine
    # For maximum speed, set headers_type=dict
    # In that case, multiple header lines will be ignored, only the first is kept
    client = HTTPClient.from_url(url, concurrency=C, headers_type=dict)
    group = gevent.pool.Pool(size=C)

    for i in range(5):
        now = time.time()
        for _ in range(N):
            group.spawn(run, client)
        group.join()

        delta = time.time() - now
        req_per_sec = N / delta

        print(f"request count:{N}, concurrenry:{C}, {req_per_sec} req/s")
