"""httpx benchmark running natively on asyncio, without gevent patching.

The other *_bench.py scripts spawn gevent greenlets around blocking
sockets; this one measures httpx in its native concurrency model instead
(see the httpx section in benchmarks/README.md).
"""

import argparse
import asyncio
import time

import httpx


async def worker(client: httpx.AsyncClient, url: str, n: int) -> None:
    for _ in range(n):
        response = await client.get(url)
        assert response.status_code == 200
        assert b"html" in response.content


async def run(url: str, concurrency: int, n: int) -> float:
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    async with httpx.AsyncClient(limits=limits) as client:
        start = time.time()
        await asyncio.gather(*(worker(client, url, n) for _ in range(concurrency)))
        return n * concurrency / (time.time() - start)


def main(n: int = 1000, concurrency: int = 10, url: str = "http://127.0.0.1/") -> None:
    req_per_sec = asyncio.run(run(url, concurrency, n))
    print(f"request count:{n * concurrency}, concurrency:{concurrency}, {req_per_sec:.2f} req/s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1/")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("-n", "--requests", type=int, default=1000, help="requests per worker")
    args = parser.parse_args()
    main(n=args.requests, concurrency=args.concurrency, url=args.url)
