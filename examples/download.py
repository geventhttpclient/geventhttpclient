#!/usr/bin/env python

import tempfile
from pathlib import Path

from geventhttpclient import UserAgent

DL_1MB = "https://proof.ovh.net/files/1Mb.dat"
DL_10MB = "https://proof.ovh.net/files/10Mb.dat"

url = DL_10MB
agent = UserAgent()


with tempfile.TemporaryDirectory() as tmp_dir:
    fpath = Path(tmp_dir) / url.rsplit("/", 1)[-1]
    print(f"Writing to {fpath}")
    agent.download(url, fpath)
    print(f"{fpath.stat().st_size} bytes downloaded")
    assert fpath.stat().st_size == 10 * 2**20  # 10 MB
