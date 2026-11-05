# HAP-python benchmarks

Performance benchmarks for the HAP accessory server. They measure the cost
of serving real HAP traffic (pair-verify, characteristic reads/writes,
subscriptions and EVENT push) from a bridge of switch accessories, plus
the memory footprint of the server process.

The benchmark drives the **real protocol**: each mock client is a real
TCP connection performing the full pair-verify handshake (X25519,
Ed25519, HKDF, ChaCha20-Poly1305) and then speaking the encrypted HAP
transport, so crypto, h11 parsing and socket costs are all included, just
like for a real iOS controller.

## Scenarios

| Scenario   | Clients | Accessories | Purpose                              |
| ---------- | ------- | ----------- | ------------------------------------ |
| `baseline` | 1       | 5           | small setup sanity floor             |
| `moderate` | 3       | 25          | typical home                         |
| `heavy`    | 5       | 250         | large installations (the pain case)   |

Each scenario measures:

- `pair_verify_ms` – session establishment latency (crypto handshake).
- `write_latency_ms` – switch toggle round trip (`PUT /characteristics`).
- `read_latency_ms` – characteristic read round trip.
- `get_accessories_ms` – full accessory list fetch (large JSON payload).
- `event_latency_ms` – write → `EVENT` frame received by the *other*
  subscribed clients. Note: value-change events are coalesced server-side
  within a 0.5 s window (`EVENT_COALESCE_TIME_WINDOW`), so this metric
  measures the coalesced delivery the way clients actually experience it.
- `writes_per_second` – sustained sequential write throughput.
- Memory (psutil RSS of the server process): at startup, after the
  clients connect, per phase, peak, and the marginal cost per accessory
  (derived from a zero-accessory probe server).

## Requirements

```sh
pip install -e ".[bench]"
```

installs the runtime dependencies plus `psutil` and `py-spy`.

## Running

From the repository root:

```sh
python benchmarks/run.py --scenario all          # all scenarios
python benchmarks/run.py --scenario heavy        # one scenario
python benchmarks/run.py --scenario heavy --flamegraph   # + py-spy flamegraph
python benchmarks/run.py --scenario heavy --tracemalloc  # + allocation sites
```

Results are written to `benchmarks/results/bench-<timestamp>.json`
(or `--output FILE` / `--results-dir DIR`). Use `--label` to tag runs, e.g.
`--label before-tuning`, so you can tell files apart.

Every run records the commit, Python version, platform, CPU, exact
parameters and all raw samples, so results can be compared over time.

### Comparing two runs

```sh
python benchmarks/compare.py results/bench-<old>.json results/bench-<new>.json
```

prints a per-scenario delta table (p50/p95 for latencies, plus throughput
and memory). To gate a change on it:

```sh
python benchmarks/compare.py base.json head.json --fail-over 20
```

exits non-zero when any latency p50 or the throughput regresses by more
than 20%. Memory is reported but never gated - it is too noisy on
shared CI runners for that.

### Flamegraphs

`--flamegraph` records the server process with `py-spy record` and
writes `benchmarks/results/<scenario>-flamegraph.svg`. The client code
lives in the orchestrator process, so the flamegraph contains only
server work.

- On Linux CI runners and most desktops this works out of the box.
- On macOS `py-spy` needs root: run the whole command under `sudo`
  (sudo the entire invocation - py-spy must spawn the server as its
  child). The shared scratch directory is made world-writable so a
  mix of uids between orchestrator and server cannot break the run;
  result and SVG files created under sudo are owned by root, so point
  `--results-dir` at a scratch directory or `chown` them after.
- `py-spy` and sudo print their own output first; the orchestrator
  skips non-JSON lines when reading the server announcement.
### Interpreting the flamegraph

py-spy samples **wall-clock** on **every thread**, so an idle server
shows up loudly. Expect three kinds of frames and read past them:

- `concurrent.futures.thread._worker` towers: pyhap keeps a
  `ThreadPoolExecutor` for state persistence; its idle worker threads
  are parked there for the whole run (in a heavy run they were ~83% of
  all samples). Not work, just thread parking.
- `KqueueSelector.select` / `EpollSelector.select` under
  `BaseEventLoop.run_forever`: the event loop waiting for I/O - in a
  heavy run this was ~92% of the main thread. The benchmark's clients
  are sequential, so the server is far from CPU-saturated.
- interpreter startup frames at the base (`<module>`, imports): the
  first ~300ms of the process. Thin in a heavy run; ignore.

What remains is the actual request-serving work. In a 5 clients / 250
accessories run it was roughly 1.2s of CPU across ~16s (~8% of one
core), dominated by asyncio socket plumbing, h11 parsing and pyhap's
`hap_protocol` dispatch - pyhap's handler, driver and crypto code was
below the one-pixel width of the graph.

For a work-only graph (drops threads that are not holding the GIL,
including the idle wait), use the passthrough flag:

```sh
python benchmarks/run.py --scenario heavy --flamegraph \
    --py-spy-args '--gil --rate 250'
```

Caveat: native code that releases the GIL (some crypto/orjson calls on
large buffers) also disappears from a `--gil` graph, so keep the
default graph around when reasoning about absolute costs.

### tracemalloc

### tracemalloc

`--tracemalloc` makes the server record the top Python allocation sites
(by line) at shutdown, stored under the scenario's `tracemalloc` key.
This distorts latency, so use it to answer "what allocates?" questions,
not "how fast?" questions.

## GitHub Actions

The `benchmarks` workflow (`.github/workflows/benchmarks.yaml`) runs on
demand (`workflow_dispatch`) with inputs:

- `base_ref` – ref to use as the baseline (default: `master`),
- `head_ref` – ref to compare (default: the ref that triggered the run),
- `scenarios` – `baseline`, `moderate`, `heavy` or `all`,
- `flamegraph` – also record flamegraphs.

It checks out both refs side by side, runs the benchmark in a separate
venv per ref, prints the comparison into the job summary and uploads the
result files and flamegraphs as artifacts.

## Caveats

- Shared CI runners have ±10-20% run-to-run noise. Trust medians and
  large deltas, not single-digit percentages.
- Clients and server communicate over loopback TCP in separate
  processes; latencies include the full socket + crypto + parsing path,
  but not physical network latency.
- The benchmark pins nothing (CPU frequency, core isolation); for
  high-precision comparisons run locally on a quiet machine.
