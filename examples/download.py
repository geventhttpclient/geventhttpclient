#!/usr/bin/env python

from gevent import monkey

monkey.patch_all()

import tempfile
from pathlib import Path

from geventhttpclient import UserAgent

DL_1MB = "https://proof.ovh.net/files/1Mb.dat"
DL_10MB = "https://proof.ovh.net/files/10Mb.dat"

url = DL_10MB


with tempfile.TemporaryDirectory() as tmp_dir:
    fpath = Path(tmp_dir) / url.rsplit("/", 1)[-1]
    print(f"Writing to {fpath}")
    UserAgent().download(url, fpath)
    print(f"{fpath.stat().st_size} bytes downloaded")
    assert fpath.stat().st_size == int(fpath.name.split("M", 1)[0]) * 2**20  # 10 MB
