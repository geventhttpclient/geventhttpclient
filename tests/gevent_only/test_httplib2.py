import gevent.pool
import pytest

import geventhttpclient.httplib

httplib2 = pytest.importorskip("geventhttpclient.httplib2")


def job(client, url):
    response, content = client.request(url)
    assert content
    assert b"body" in content


@pytest.mark.network
def test_request_parallel():
    with geventhttpclient.httplib.patched():
        errors = []

        client = httplib2.Http(concurrency=5)
        group = gevent.pool.Pool(size=5)

        urls = ["https://google.com", "http://gevent.org", "https://github.com"]
        for url in urls:
            g = group.spawn(job, client, url)
            g.link_exception(lambda g: errors.append(g.exception))
        group.join()
        if errors:
            for e in errors:
                print(e)
            raise errors[0]
