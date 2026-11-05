"""Benchmark orchestrator: spawn the server, drive mock clients, record metrics.

Usage (from the repository root)::

    python benchmarks/run.py --scenario all
    python -m benchmarks.run --scenario heavy --flamegraph

The orchestrator spawns ``benchmarks/server.py`` as a child process (so
flamegraphs profile the server alone), connects the configured number of
mock controllers over loopback TCP and measures:

* ``pair_verify_ms`` - session establishment (crypto handshake).
* ``write_latency_ms`` - switch toggle round trip (PUT /characteristics).
* ``read_latency_ms`` - characteristic read round trip.
* ``get_accessories_ms`` - full accessory list fetch (large payload).
* ``event_latency_ms`` - write -> EVENT frame received by the other
  subscribed clients (value changes are coalesced server-side within a
  0.5s window, which dominates this metric).
* ``writes_per_second`` - sustained sequential write throughput.

Memory of the server process is sampled with psutil; a probe server with
zero accessories is also spawned to derive the marginal cost per
accessory.

Results are written as JSON, one file per run, which
``benchmarks/compare.py`` can diff.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import datetime as datetime_module
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

try:
    from benchmarks.client import BenchController, ClientIdentity
except ImportError:  # running as a plain script, not ``-m benchmarks.run``
    sys.path.insert(0, str(REPO_ROOT))
    from benchmarks.client import BenchController, ClientIdentity

try:
    import psutil
except ImportError as import_error:
    raise SystemExit(
        "The benchmark requires psutil. Install it with: pip install '.[bench]'"
    ) from import_error

SERVER_SCRIPT = Path(__file__).resolve().parent / "server.py"


def _safe_kill(process: asyncio.subprocess.Process) -> None:
    """Kill a child process, tolerating one that already exited."""
    try:
        process.kill()
    except ProcessLookupError:
        pass


RESULTS_DIR = REPO_ROOT / "benchmarks" / "results"
DEFAULT_RESULTS_DIR = RESULTS_DIR

# Events are coalesced server-side within this window; wait slightly
# longer before draining the event queues between phases.
_EVENT_SETTLE_SECONDS = 0.7

_WARMUP_WRITES = 20
_WARMUP_READS = 10

# How long to wait for the server child to print its announcement line.
_ANNOUNCE_TIMEOUT_S = 60


@dataclass(frozen=True)
class Scenario:
    """The parameters of one benchmark scenario."""

    name: str
    clients: int
    accessories: int
    write_ops: int
    read_ops: int
    accessories_ops: int
    event_ops: int
    throughput_seconds: float = 3.0


SCENARIOS: dict[str, Scenario] = {
    "baseline": Scenario(
        name="baseline",
        clients=1,
        accessories=5,
        write_ops=500,
        read_ops=300,
        accessories_ops=100,
        event_ops=40,
    ),
    "moderate": Scenario(
        name="moderate",
        clients=3,
        accessories=25,
        write_ops=400,
        read_ops=200,
        accessories_ops=100,
        event_ops=30,
    ),
    "heavy": Scenario(
        name="heavy",
        clients=5,
        accessories=250,
        write_ops=300,
        read_ops=100,
        accessories_ops=50,
        event_ops=20,
    ),
}


# ### Helpers ###


def summarize(samples_ms: list[float]) -> dict[str, Any]:
    """Summarize a list of latency samples in milliseconds."""
    if not samples_ms:
        return {"count": 0, "samples": []}
    ordered = sorted(samples_ms)
    count = len(ordered)

    def percentile(fraction: float) -> float:
        return ordered[min(count - 1, round(count * fraction))]

    return {
        "count": count,
        "mean": round(statistics.fmean(ordered), 3),
        "p50": round(percentile(0.50), 3),
        "p95": round(percentile(0.95), 3),
        "p99": round(percentile(0.99), 3),
        "min": round(ordered[0], 3),
        "max": round(ordered[-1], 3),
        "stddev": round(statistics.pstdev(ordered), 3),
        "samples": [round(value, 3) for value in samples_ms],
    }


def git_commit() -> str:
    """Return the current commit, preferring the CI override."""
    override = os.environ.get("HAP_BENCH_COMMIT")
    if override:
        return override
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        return subprocess.run(
            [git, "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def cpu_model() -> str:
    """Best-effort CPU model name for the result metadata."""
    if sys.platform == "darwin":
        sysctl = shutil.which("sysctl")
        if sysctl is not None:
            try:
                return subprocess.run(
                    [sysctl, "-n", "machdep.cpu.brand_string"],
                    capture_output=True,
                    check=True,
                    text=True,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
    if sys.platform.startswith("linux"):
        try:
            for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    return platform.processor() or "unknown"


def child_environment() -> dict[str, str]:
    """Environment for the server child, with the source checkout first."""
    env = os.environ.copy()
    python_path = env.get("PYTHONPATH", "")
    repo = str(REPO_ROOT)
    if repo not in python_path.split(os.pathsep):
        env["PYTHONPATH"] = repo + (os.pathsep + python_path if python_path else "")
    return env


# All live server handles, so that nothing leaks if a scenario crashes
# mid-run (see async_main's finally).
_ACTIVE_SERVERS: list[ServerHandle] = []


class ServerHandle:
    """A spawned benchmark server child process."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        info: dict[str, Any],
        command: list[str],
    ) -> None:
        """Store the process and its announcement data.

        ``info`` is the JSON line the server prints once listening; the
        ``pid`` in it is the real server process even when py-spy wraps
        it, because signals must reach the server itself.
        """
        self._process = process
        self._command = command
        self._stopped = False
        self.info = info
        _ACTIVE_SERVERS.append(self)

    @property
    def pid(self) -> int:
        """The OS pid of the (innermost) server process."""
        return int(self.info["pid"])

    async def stop(self) -> None:
        """SIGTERM the server and wait for the process tree to exit."""
        if self._stopped:
            return
        self._stopped = True
        if self in _ACTIVE_SERVERS:
            _ACTIVE_SERVERS.remove(self)
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            print(f"could not signal server {self.pid}: {error}", file=sys.stderr)
        try:
            await asyncio.wait_for(self._process.wait(), timeout=20)
        except TimeoutError:
            print(
                "server did not exit after SIGTERM; killing "
                f"(command: {' '.join(self._command)})",
                file=sys.stderr,
            )
            try:
                self._process.kill()
            except ProcessLookupError:
                return
            await self._process.wait()


async def spawn_server(
    accessories: int,
    clients_file: str,
    state_dir: str,
    flamegraph: Path | None = None,
    tracemalloc_out: Path | None = None,
) -> ServerHandle:
    """Spawn ``benchmarks/server.py``, optionally under py-spy record."""
    command = [
        sys.executable,
        str(SERVER_SCRIPT),
        "--accessories",
        str(accessories),
        "--clients-file",
        clients_file,
        "--state-dir",
        state_dir,
    ]
    if tracemalloc_out is not None:
        command += ["--tracemalloc-out", str(tracemalloc_out)]
    if flamegraph is not None:
        pyspy = shutil.which("py-spy")
        if pyspy is None:
            raise SystemExit(
                "py-spy is required for --flamegraph. "
                "Install it with: pip install '.[bench]' "
                "(on macOS py-spy also needs to run under sudo)."
            )
        command = [
            pyspy,
            "record",
            "--format",
            "flamegraph",
            "-o",
            str(flamegraph),
            "--",
            *command,
        ]
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,
        env=child_environment(),
    )
    assert process.stdout is not None  # noqa: S101

    # Read the JSON announcement line. py-spy (and sudo) may print their own
    # output to the same pipe first, so non-JSON lines are skipped.
    deadline = asyncio.get_running_loop().time() + _ANNOUNCE_TIMEOUT_S
    info: dict[str, Any] | None = None
    while info is None:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            _safe_kill(process)
            raise SystemExit(
                f"Benchmark server did not announce itself in time "
                f"(command: {' '.join(command)})"
            )
        try:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
        except TimeoutError as err:
            _safe_kill(process)
            raise SystemExit(
                f"Benchmark server did not announce itself in time "
                f"(command: {' '.join(command)})"
            ) from err
        if not line:
            await process.wait()
            raise SystemExit(
                f"Benchmark process exited before announcing itself "
                f"(command: {' '.join(command)}). "
                + (
                    "py-spy requires root on macOS - run the benchmark under sudo."
                    if flamegraph is not None
                    else ""
                )
            )
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            print(
                f"benchmark wrapper output: {line.decode(errors='replace').strip()}",
                file=sys.stderr,
            )
    if "error" in info:
        await process.wait()
        raise SystemExit(f"Benchmark server failed to start: {info['error']}")
    return ServerHandle(process, info, command)


class MemorySampler:
    """Samples the server RSS in the background and tags phase boundaries."""

    def __init__(self, pid: int) -> None:
        """Prepare sampling of the process with the given pid."""
        self._process = psutil.Process(pid)
        self._task: asyncio.Task[None] | None = None
        self.samples: list[tuple[float, int]] = []
        self.marks: list[tuple[float, str]] = []

    def start(self) -> None:
        """Start sampling every 50 ms."""
        self.marks.append((time.perf_counter(), "start"))
        self._task = asyncio.create_task(self._sample_loop())

    async def _sample_loop(self) -> None:
        while True:
            try:
                self.samples.append(
                    (time.perf_counter(), self._process.memory_info().rss)
                )
            except psutil.Error:
                return
            await asyncio.sleep(0.05)

    def mark(self, name: str) -> None:
        """Tag a phase boundary for the sample timeline."""
        self.marks.append((time.perf_counter(), name))

    async def stop(self) -> None:
        """Stop sampling."""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def phase_summary(self) -> dict[str, dict[str, int]]:
        """Mean and peak RSS per phase, based on the marks."""
        if not self.samples:
            return {}
        summary: dict[str, dict[str, int]] = {}
        # marks and marks[1:] intentionally differ in length by one.
        for (start, name), (end, _) in zip(self.marks, self.marks[1:], strict=False):
            in_phase = [
                rss for timestamp, rss in self.samples if start <= timestamp < end
            ]
            if not in_phase:
                continue
            summary[name] = {
                "mean_rss_bytes": sum(in_phase) // len(in_phase),
                "peak_rss_bytes": max(in_phase),
            }
        return summary

    @property
    def peak_rss(self) -> int:
        """The highest RSS observed."""
        return max((rss for _, rss in self.samples), default=0)


# ### Scenario runner ###


async def run_scenario(scenario: Scenario, args: argparse.Namespace) -> dict[str, Any]:
    """Run one scenario end to end and return its result dict."""
    print(
        f"== scenario {scenario.name}: "
        f"{scenario.clients} clients / {scenario.accessories} accessories"
    )
    started_at = time.perf_counter()

    identities = [ClientIdentity.generate(i) for i in range(scenario.clients)]

    with tempfile.TemporaryDirectory(prefix="hap-bench-") as state_dir:
        clients_file = str(Path(state_dir) / "clients.json")
        with open(clients_file, "w", encoding="utf-8") as file_handle:
            json.dump(
                [
                    {
                        "uuid": identity.uuid,
                        "public_key": identity.public_key_bytes.hex(),
                    }
                    for identity in identities
                ],
                file_handle,
            )

        # A probe with zero accessories isolates the per-accessory memory
        # cost from the interpreter/driver baseline.
        probe = await spawn_server(0, clients_file, state_dir)
        await asyncio.sleep(0.3)
        probe_rss = psutil.Process(probe.pid).memory_info().rss
        await probe.stop()

        tracemalloc_out: Path | None = None
        if args.tracemalloc:
            tracemalloc_out = Path(state_dir) / "tracemalloc.json"
        flamegraph = (
            args.results_dir / f"{scenario.name}-flamegraph.svg"
            if args.flamegraph
            else None
        )

        server = await spawn_server(
            scenario.accessories,
            clients_file,
            state_dir,
            flamegraph=flamegraph,
            tracemalloc_out=tracemalloc_out,
        )
        port = int(server.info["port"])
        ltpk = bytes.fromhex(server.info["accessory_ltpk"])
        switches: list[dict[str, int]] = server.info["switches"]
        if not switches:
            raise SystemExit("Benchmark server has no switches to toggle")

        sampler = MemorySampler(server.pid)
        sampler.start()
        startup_rss = psutil.Process(server.pid).memory_info().rss

        # ### Connect and pair-verify ###
        sampler.mark("connect")
        clients: list[BenchController] = []
        pair_verify_ms: list[float] = []
        for identity in identities:
            controller = BenchController(identity, ltpk)
            started = time.perf_counter()
            await controller.connect("127.0.0.1", port)
            pair_verify_ms.append((time.perf_counter() - started) * 1000)
            clients.append(controller)
        post_connect_rss = psutil.Process(server.pid).memory_info().rss

        # Subscribe every client to every switch, like a real controller.
        for controller in clients:
            for switch in switches:
                await controller.subscribe(switch["aid"], switch["iid"])

        # ### Warmup ###
        sampler.mark("warmup")
        for index in range(_WARMUP_WRITES):
            controller = clients[index % len(clients)]
            switch = switches[index % len(switches)]
            await controller.write_characteristic(
                switch["aid"], switch["iid"], bool(index % 2)
            )
        for index in range(_WARMUP_READS):
            controller = clients[index % len(clients)]
            switch = switches[index % len(switches)]
            await controller.read_characteristic(switch["aid"], switch["iid"])

        # Let the last coalesced events flush, then drop them so the
        # event phase only sees fresh events.
        await asyncio.sleep(_EVENT_SETTLE_SECONDS)
        for controller in clients:
            controller.clear_events()

        # ### Timed phases ###
        sampler.mark("write")
        write_ms: list[float] = []
        for index in range(scenario.write_ops):
            controller = clients[index % len(clients)]
            switch = switches[index % len(switches)]
            started = time.perf_counter_ns()
            await controller.write_characteristic(
                switch["aid"], switch["iid"], bool(index % 2)
            )
            write_ms.append((time.perf_counter_ns() - started) / 1e6)

        sampler.mark("read")
        read_ms: list[float] = []
        for index in range(scenario.read_ops):
            controller = clients[index % len(clients)]
            switch = switches[index % len(switches)]
            started = time.perf_counter_ns()
            frame = await controller.read_characteristic(switch["aid"], switch["iid"])
            read_ms.append((time.perf_counter_ns() - started) / 1e6)
            if frame.status != 200:
                raise SystemExit(
                    f"Read of {switch} failed: {frame.status} {frame.body!r}"
                )

        sampler.mark("get_accessories")
        accessories_ms: list[float] = []
        accessories_bytes: list[int] = []
        for index in range(scenario.accessories_ops):
            controller = clients[index % len(clients)]
            started = time.perf_counter_ns()
            frame = await controller.get_accessories()
            accessories_ms.append((time.perf_counter_ns() - started) / 1e6)
            accessories_bytes.append(len(frame.body))
            if frame.status != 200:
                raise SystemExit(
                    f"GET /accessories failed: {frame.status} {frame.body!r}"
                )

        sampler.mark("event")
        event_ms: list[float] = []
        if scenario.clients >= 2:
            # Drain events still pending from the write phase (they are
            # coalesced server-side within a 0.5s window) so waiters only
            # ever see events for the writes made here.
            await asyncio.sleep(_EVENT_SETTLE_SECONDS)
            for controller in clients:
                controller.clear_events()
            for index in range(scenario.event_ops):
                writer = clients[index % len(clients)]
                waiters = [client for client in clients if client is not writer]
                switch = switches[index % len(switches)]
                # The server only publishes events when a value actually
                # changes, so toggle the current state instead of writing
                # a fixed pattern that might be a no-op.
                frame = await writer.read_characteristic(switch["aid"], switch["iid"])
                current = json.loads(frame.body)["characteristics"][0]["value"]
                value = not current
                started = time.perf_counter_ns()
                await writer.write_characteristic(switch["aid"], switch["iid"], value)
                for waiter in waiters:
                    received_ns = await waiter.wait_event(
                        switch["aid"], switch["iid"], value, timeout=5.0
                    )
                    event_ms.append((received_ns - started) / 1e6)

        sampler.mark("throughput")
        writer = clients[0]
        switch = switches[0]
        loop = asyncio.get_running_loop()
        deadline = loop.time() + scenario.throughput_seconds
        writes = 0
        while loop.time() < deadline:
            await writer.write_characteristic(
                switch["aid"], switch["iid"], not bool(writes % 2)
            )
            writes += 1
        writes_per_second = writes / scenario.throughput_seconds

        sampler.mark("done")
        phase_rss = sampler.phase_summary()
        peak_rss = sampler.peak_rss
        await sampler.stop()

        for controller in clients:
            await controller.close()
        await server.stop()

        tracemalloc_data: list[dict[str, Any]] | None = None
        if tracemalloc_out is not None and tracemalloc_out.exists():
            tracemalloc_data = json.loads(tracemalloc_out.read_text(encoding="utf-8"))

    duration_s = time.perf_counter() - started_at
    marginal_bytes = (
        (startup_rss - probe_rss) // scenario.accessories if scenario.accessories else 0
    )

    result: dict[str, Any] = {
        "scenario": {
            "name": scenario.name,
            "clients": scenario.clients,
            "accessories": scenario.accessories,
            "ops": {
                "write": scenario.write_ops,
                "read": scenario.read_ops,
                "get_accessories": scenario.accessories_ops,
                "event": scenario.event_ops,
                "throughput_seconds": scenario.throughput_seconds,
            },
        },
        "metrics": {
            "pair_verify_ms": summarize(pair_verify_ms),
            "write_latency_ms": summarize(write_ms),
            "read_latency_ms": summarize(read_ms),
            "get_accessories_ms": summarize(accessories_ms),
            "event_latency_ms": summarize(event_ms),
        },
        "scalars": {
            "writes_per_second": round(writes_per_second, 1),
            "get_accessories_response_bytes": (
                sum(accessories_bytes) // len(accessories_bytes)
                if accessories_bytes
                else 0
            ),
            "accessory_build_ms": server.info["build_ms"],
        },
        "memory": {
            "probe_rss_bytes": probe_rss,
            "startup_rss_bytes": startup_rss,
            "post_connect_rss_bytes": post_connect_rss,
            "peak_rss_bytes": peak_rss,
            "marginal_bytes_per_accessory": marginal_bytes,
            "phases": phase_rss,
        },
        "tracemalloc": tracemalloc_data,
        "duration_s": round(duration_s, 1),
    }
    if flamegraph is not None and flamegraph.exists():
        print(f"   flamegraph: {flamegraph}")
    return result


# ### CLI ###


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Run the HAP-python server benchmark.")
    parser.add_argument(
        "--scenario",
        default="all",
        help="baseline, moderate, heavy or all (default: all)",
    )
    parser.add_argument("--clients", type=int, help="override the client count")
    parser.add_argument("--accessories", type=int, help="override the accessory count")
    parser.add_argument(
        "--write-ops", type=int, help="override the number of timed writes"
    )
    parser.add_argument(
        "--event-ops",
        type=int,
        help="override the number of event propagation measurements "
        "(each takes up to 0.5s due to server-side event coalescing)",
    )
    parser.add_argument(
        "--flamegraph",
        action="store_true",
        help="record a py-spy flamegraph of the server (one per scenario)",
    )
    parser.add_argument(
        "--tracemalloc",
        action="store_true",
        help="also enable tracemalloc in the server and record the top "
        "allocation sites (slows the server down)",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIR,
        help=f"directory for result files (default: {DEFAULT_RESULTS_DIR})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="exact result file path (default: <results-dir>/bench-<timestamp>.json)",
    )
    parser.add_argument("--label", default="", help="label stored in the result meta")
    return parser.parse_args(argv)


def selected_scenarios(args: argparse.Namespace) -> list[Scenario]:
    """Resolve the scenarios to run, applying overrides."""
    names = (
        list(SCENARIOS)
        if args.scenario == "all"
        else [name.strip() for name in args.scenario.split(",")]
    )
    scenarios = []
    for name in names:
        if name not in SCENARIOS:
            raise SystemExit(
                f"Unknown scenario: {name!r} (choose from {list(SCENARIOS)})"
            )
        overrides = {}
        for field_name, arg_name in (
            ("clients", "clients"),
            ("accessories", "accessories"),
            ("write_ops", "write_ops"),
            ("event_ops", "event_ops"),
        ):
            override = getattr(args, arg_name)
            if override is not None:
                overrides[field_name] = override
        scenarios.append(
            Scenario(**{**SCENARIOS[name].__dict__, **overrides})
            if overrides
            else SCENARIOS[name]
        )
    return scenarios


def build_meta(args: argparse.Namespace) -> dict[str, Any]:
    """Metadata describing the code and machine the run was made on."""
    return {
        "benchmark_version": 1,
        "timestamp": datetime_module.datetime.now(
            datetime_module.timezone.utc
        ).isoformat(),
        "commit": git_commit(),
        "label": args.label,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "cpu": cpu_model(),
        "argv": sys.argv[1:],
    }


async def async_main(argv: list[str] | None) -> int:
    """Entry point of the orchestrator."""
    args = parse_args(argv)
    scenarios = selected_scenarios(args)
    args.results_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Any] = {}
    for scenario in scenarios:
        try:
            results[scenario.name] = await run_scenario(scenario, args)
        finally:
            # Never leak server children, even when a scenario crashes
            # mid-run or the run is interrupted.
            for handle in list(_ACTIVE_SERVERS):
                await handle.stop()

    document = {"meta": build_meta(args), "scenarios": results}
    output = args.output
    if output is None:
        timestamp = datetime_module.datetime.now().strftime("%Y%m%d-%H%M%S")
        label = f"-{args.label}" if args.label else ""
        output = args.results_dir / f"bench{label}-{timestamp}.json"
    output.write_text(json.dumps(document, indent=2), encoding="utf-8")

    for scenario in scenarios:
        metrics = results[scenario.name]["metrics"]
        print(
            f"== {scenario.name}: "
            f"write p50/p95 = {metrics['write_latency_ms']['p50']}"
            f"/{metrics['write_latency_ms']['p95']} ms, "
            f"get_accessories p50 = {metrics['get_accessories_ms']['p50']} ms, "
            f"wps = {results[scenario.name]['scalars']['writes_per_second']}"
        )
    print(f"results written to {output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Synchronous entry point."""
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
