import gevent.monkey

gevent.monkey.patch_all()

import argparse
import time

import requests
import requests.adapters
import gevent.pool
import geventhttpclient.useragent
import urllib3


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

    def start(self):
        results = []
        for round in range(self.rounds):
            self.init_client()

            now = time.time()

            pool = gevent.pool.Pool(size=self.concurrency)
            for _ in range(self.round_size):
                pool.spawn(self.request)
            pool.join()

            delta = time.time() - now
            rps = self.round_size / delta
            results.append(rps)

            print(f"round: {round}, rps: {rps}")
        print("total rps:", sum(results) / len(results))


class GeventHTTPClientBenchmark(Benchmark):
    client: geventhttpclient.useragent.UserAgent

    def init_client(self):
        self.client = geventhttpclient.useragent.UserAgent(concurrency=self.concurrency)

    def request(self):
        self.client.urlopen(self.url).content


class RequestsBenchmark(Benchmark):
    client: requests.Session

    def init_client(self):
        self.client = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_maxsize=self.concurrency, pool_block=True)
        self.client.mount("https://", adapter)
        self.client.mount("http://", adapter)

    def request(self):
        self.client.get(self.url)


class UrllibBenchmark(Benchmark):
    client: urllib3.PoolManager

    def init_client(self):
        self.client = urllib3.PoolManager(maxsize=self.concurrency, block=True)

    def request(self):
        self.client.request("GET", self.url)


if __name__ == "__main__":
    available_benchmarks = {
        "gevent": GeventHTTPClientBenchmark,
        "requests": RequestsBenchmark,
        "urllib": UrllibBenchmark,
    }

    parser = argparse.ArgumentParser()
    parser.add_argument(dest="url")
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--round-size", type=int, default=10000)
    parser.add_argument(
        "-b",
        "--benchmark",
        nargs="+",
        choices=available_benchmarks.keys(),
        default=available_benchmarks.keys(),
    )
    args = dict(**parser.parse_args().__dict__)

    benchmark_classes = (available_benchmarks[x] for x in args.pop("benchmark"))

    for benchmark_class in benchmark_classes:
        print(f"Running {benchmark_class.__name__}")
        benchmark = benchmark_class(**args)
        benchmark.start()
        print()
