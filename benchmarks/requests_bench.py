import time

import gevent.monkey
import gevent.pool

gevent.monkey.patch_all()

import requests

if __name__ == "__main__":
    N = 1000
    C = 10

    url = "http://127.0.0.1/index.html"

    def run(client):
        response = client.get(url)
        assert response.status_code == requests.codes.ok

    client = requests.Session()
    group = gevent.pool.Pool(size=C)

    for i in range(5):
        now = time.time()
        for _ in range(N):
            group.spawn(run, client)
        group.join()

        delta = time.time() - now
        req_per_sec = N / delta

        print(f"request count:{N}, concurrenry:{C}, {req_per_sec} req/s")
