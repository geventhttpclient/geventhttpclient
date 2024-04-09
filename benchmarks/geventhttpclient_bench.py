import time

import gevent.pool

from geventhttpclient import URL, HTTPClient

N = 1000
C = 10

url = URL("http://127.0.0.1/")
qs = url.request_uri


def run(client):
    response = client.get(qs)
    response.read()
    assert response.status_code == 200


client = HTTPClient.from_url(url, concurrency=C)
group = gevent.pool.Pool(size=C)
run(client)

for i in range(5):
    now = time.time()
    for _ in range(N):
        group.spawn(run, client)
    group.join()

    delta = time.time() - now
    req_per_sec = N / delta

    print(f"request count:{N}, concurrenry:{C}, {req_per_sec} req/s")
