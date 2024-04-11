import gevent.pool
import pytest

httplib2 = pytest.importorskip("geventhttpclient.httplib2")


def job(client, url):
    response, content = client.request(url)
    assert content
    assert b"body" in content


@pytest.mark.network
def test_request_parallel():
    client = httplib2.Http(concurrency=5)
    group = gevent.pool.Pool(size=5)

    urls = ["https://google.com", "https://gevent.org", "https://github.com"] * 3
    for url in urls:
        group.spawn(job, client, url)
