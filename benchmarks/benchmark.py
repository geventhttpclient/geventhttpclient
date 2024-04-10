import gevent.monkey

gevent.monkey.patch_all()

import geventhttpclient.httplib

geventhttpclient.httplib.patch()

import argparse
import platform
import sys
import time

import gevent.pool
import httpx
import requests
import requests.adapters
import urllib3

from geventhttpclient import httplib2, useragent


class Benchmark:
    def __init__(self, url: str, concurrency: int, rounds: int, round_size: int):
        self.url = url
        self.concurrency = concurrency
        self.rounds = rounds
        self.round_size = round_size

        self.init_client()

    def init_client(self):
        pass

    def request(self):
        pass

    def request_with_check(self):
        content = self.request()
        assert content
        assert b"html" in content

    def start(self):
        results = []
        for round in range(1, self.rounds + 1):
            self.init_client()

            now = time.time()

            pool = gevent.pool.Pool(size=self.concurrency)
            for _ in range(self.round_size):
                pool.spawn(self.request_with_check)
            pool.join()

            delta = time.time() - now
            rps = self.round_size / delta
            results.append(rps)

            print(f"round: {round}, rps: {rps:.1f}")
        print(f"total rps:     {sum(results) / len(results):.1f}")


class GeventHTTPClientBenchmark(Benchmark):
    client: useragent.UserAgent

    def init_client(self):
        self.client = useragent.UserAgent(concurrency=self.concurrency)

    def request(self):
        return self.client.urlopen(self.url).content


class RequestsBenchmark(Benchmark):
    client: requests.Session

    def init_client(self):
        self.client = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_maxsize=self.concurrency, pool_block=True)
        self.client.mount("https://", adapter)
        self.client.mount("http://", adapter)

    def request(self):
        return self.client.get(self.url).content


class HttpxBenchmark(Benchmark):
    client: httpx.Client

    def init_client(self):
        # TODO: This should run async
        self.client = httpx.Client()

    def request(self):
        return self.client.get(self.url).content


class Httplib2Benchmark(Benchmark):
    client: httplib2.Http

    def init_client(self):
        self.client = httplib2.Http(concurrency=self.concurrency)

    def request(self):
        response, content = self.client.request(self.url)
        return content


class Urllib3Benchmark(Benchmark):
    client: urllib3.PoolManager

    def init_client(self):
        self.client = urllib3.PoolManager(maxsize=self.concurrency, block=True)

    def request(self):
        return self.client.request("GET", self.url).data


available_benchmarks = {
    "gevent": GeventHTTPClientBenchmark,
    "httpx": HttpxBenchmark,
    "requests": RequestsBenchmark,
    "urllib": Urllib3Benchmark,
    "httplib2": Httplib2Benchmark,
}


def arg_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1/")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--round-size", type=int, default=10000)
    parser.add_argument(
        "-b",
        "--benchmark",
        nargs="+",
        choices=available_benchmarks.keys(),
        default=available_benchmarks.keys(),
    )
    return parser


def main():
    args = arg_parser().parse_args().__dict__
    benchmark_classes = (available_benchmarks[x] for x in args.pop("benchmark"))
    for benchmark_class in benchmark_classes:
        print(f"Running {benchmark_class.__name__}".removesuffix("Benchmark"))
        benchmark = benchmark_class(**args)
        benchmark.start()
        print()
    print(f"{platform.system()}({platform.machine()}), Python {sys.version.split()[0]}")


if __name__ == "__main__":
    main()
