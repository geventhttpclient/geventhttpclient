# Benchmarking note

The benchmarks in this directory are mainly intended to run against a
fast local HTTP server like nginx. Such a setup is not fully comparable
with connections over a larger network with longer response times and
lower bandwidth.

A special observation of this is, that disabling monkey patching and
therefore in fact running all requests sequentially might perform better
than with monkey patching enabled! The requests per second, i.e. the
performance is in this case limited by the message parsing efficiency of
the client implementation and not by reducing the delay of sequential
waiting for multiple slow connections over the network.

Running the benchmark with monkey patching enabled is what you might
usually be interested in, as this will be more comparable to real
life applications over the internet. Nevertheless, some clients seem to
be more affected by the monkey patching than others. Especially httpx
performance seems to suffer a lot when running monkey patched and should
better be run using a separate async benchmark for a fair comparison.

So as always, please take the results of our benchmarking with the necessary
grain of salt! The results might be very different for other use-cases.
