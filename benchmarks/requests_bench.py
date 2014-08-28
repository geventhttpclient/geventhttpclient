import time
import gevent.pool
import gevent.monkey

gevent.monkey.patch_all()

import requests

if __name__ == "__main__":

    N = 1000
    C = 10

    url = 'http://127.0.0.1/index.html'

    def run(client):
        response = client.get(url)
        assert response.status_code == requests.codes.ok

    client = requests.Session()
    group = gevent.pool.Pool(size=C)

    for i in xrange(5):
        now = time.time()
        for _ in xrange(N):
            group.spawn(run, client)
        group.join()
    
        delta = time.time() - now
        req_per_sec = N / delta
    
        print "request count:%d, concurrenry:%d, %f req/s" % (
        N, C, req_per_sec)


