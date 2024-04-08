#!/usr/bin/env python

from geventhttpclient import URL, HTTPClient

if __name__ == "__main__":
    url = URL("http://127.0.0.1:80/100.dat")
    http = HTTPClient.from_url(url)
    response = http.get(url.request_uri)
    assert response.status_code == 200

    CHUNK_SIZE = 1024 * 16  # 16KB
    with open("/tmp/100.dat", "w") as f:
        data = response.read(CHUNK_SIZE)
        while data:
            f.write(data)
            data = response.read(CHUNK_SIZE)
