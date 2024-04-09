#!/usr/bin/env python

from gevent import monkey

monkey.patch_all()

import tempfile

from geventhttpclient import URL, HTTPClient

DL_1MB = "https://proof.ovh.net/files/1Mb.dat"
DL_10MB = "https://proof.ovh.net/files/10Mb.dat"

url = URL(DL_10MB)
http = HTTPClient.from_url(url)

CHUNK_SIZE = 1024 * 16  # 16KB
with http.get(url.request_uri) as response:
    assert response.status_code == 200
    with tempfile.NamedTemporaryFile() as f:
        print(f"Writing to {f.name}")
        data = response.read(CHUNK_SIZE)
        while data:
            f.write(data)
            data = response.read(CHUNK_SIZE)
        print(f"{f.tell()} bytes downloaded")
        assert f.tell() == 10 * 2**20  # 10 MB
