# Benchmarking notes

## What these benchmarks measure

All harnesses in this directory drive clients against a fast local server —
nginx on `127.0.0.1` is the intended setup. That choice defines the load
regime, and the regime decides what the numbers mean:

- **Loopback**: round-trip times are in the microsecond range, so the network
  is never the bottleneck. Requests per second are limited by the per-request
  CPU cost of the client itself: HTTP message parsing, header object
  construction, connection-pool bookkeeping and — when monkey patched —
  greenlet scheduling. Numbers from this regime are a client-efficiency
  comparison, not a prediction of real-world latency.
- **Real network**: once round-trip latency is measurable in tens of
  milliseconds, wall-clock time is dominated by waiting for the network.
  Concurrent requests overlap that wait, and the differences between clients
  collapse: a client that is several times faster on loopback is typically
  within 1.0–1.5× of the others over a real network.

We measured exactly this transition (Python 3.14, gevent 26.9, nginx 1.24 on
`127.0.0.1`, 10 concurrent keep-alive connections, small static page;
absolute numbers are machine-specific, the ratios are the point):

| client           | loopback, patched | loopback, unpatched | 20 ms latency, patched |
| ---------------- | ----------------: | ------------------: | ---------------------: |
| geventhttpclient |              5600 |                5970 |                    458 |
| httplib2         |              1990 |                2000 |                    435 |
| urllib3          |              1630 |                2510 |                    380 |
| requests         |               957 |                1210 |                    322 |
| httpx (sync)     |               783 |                1460 |                    434 |

On loopback the client is the bottleneck and geventhttpclient's C parser
(llhttp) puts it well ahead. Under simulated latency (20 ms added per
response, 10 connections → ~500 requests/s ceiling) every client converges
towards that ceiling, and the loopback advantage largely disappears.

## Monkey patching

`benchmark.py` calls `gevent.monkey.patch_all()` before anything else: all
clients then run as greenlets cooperatively scheduled on the gevent hub, the
setup of a typical gevent application. Concurrency overlaps I/O waits at the
cost of per-request scheduling overhead (greenlet switches, event-loop
bookkeeping).

The standalone `*_bench.py` scripts do **not** patch. Sockets stay blocking,
so the greenlets in the pool effectively run one after another. With no waits
to overlap, the hub overhead buys nothing, and against a fast local server
every client runs faster unpatched than patched (in our measurements by 6% to
86%, httpx gaining the most). That is the scheduling overhead made visible,
not a client deficiency.

As a rule of thumb:

- patched (`benchmark.py`): the realistic setup for gevent applications, and
  the only setup where concurrency can pay off over a real network
- unpatched (`*_bench.py`): a rough client-side CPU benchmark per request

## httpx

httpx is asyncio-native. Running its synchronous API under gevent monkey
patching penalizes it disproportionately (see the loopback numbers above),
and its native async mode on an asyncio event loop performs in a different
range again (~600–680 requests/s in our setup). For a fair httpx comparison,
benchmark it with its own async API rather than through this gevent harness.

## Reading the numbers

These measurements cover one workload: small responses, high request rates,
loopback latency. Large downloads are bandwidth-bound, a handful of requests
is latency-bound — both will rank clients differently. Take everything here
with the appropriate grain of salt and, ideally, re-run against a workload
that resembles your own.
