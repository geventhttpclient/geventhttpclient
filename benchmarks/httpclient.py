import time
import gevent.pool
from geventhttpclient import HTTPClient, URL
from geventhttpclient.header import Headers


if __name__ == "__main__":

    N = 1000
    C = 10

    url = URL('http://127.0.0.1/index.html')
    qs = url.request_uri

    def run(client):
        response = client.get(qs)
        response.read()
        assert response.status_code == 200


    client = HTTPClient.from_url(url, concurrency=C, headers_type=Headers)
    group = gevent.pool.Pool(size=C)

    now = time.time()
    for _ in xrange(N):
        group.spawn(run, client)
    group.join()

    delta = time.time() - now
    req_per_sec = N / delta

    print "request count:%d, concurrenry:%d, %f req/s" % (
    N, C, req_per_sec)


