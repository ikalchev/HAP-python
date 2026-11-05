"""Benchmark suite for the HAP-python accessory server.

See ``benchmarks/README.md`` for usage. The modules are:

* ``benchmarks.client``: a mock HAP controller (client) speaking the real
  protocol: pair-verify, encrypted transport and EVENT push events.
* ``benchmarks.server``: the benchmark accessory server, run as a child
  process by the orchestrator.
* ``benchmarks.run``: CLI orchestrator producing JSON result files.
* ``benchmarks.compare``: A/B comparison of two result files.
"""
