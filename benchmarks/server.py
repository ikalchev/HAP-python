"""The benchmark HAP accessory server.

This module is spawned as a child process by ``benchmarks/run.py``. It
builds a bridge with a configurable number of switch accessories,
pre-pairs a configurable number of mock controllers (bypassing the slow
SRP pair-setup) and runs the real HAP server on an ephemeral loopback
port - without mDNS advertising.

Once listening, a single JSON line with the connection details is
printed to stdout; everything else (logs) goes to stderr. The process is
stopped with SIGTERM, which gracefully stops the accessory driver.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import socket
import sys
import tempfile
import time
import tracemalloc
from typing import Any, cast

# Make the source checkout importable when run as a plain script from
# anywhere (e.g. inside a benchmark venv or a workflow checkout).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from cryptography.hazmat.primitives import serialization  # noqa: E402

from pyhap.accessory import Accessory, Bridge  # noqa: E402
from pyhap.accessory_driver import AccessoryDriver  # noqa: E402
from pyhap.const import CATEGORY_SWITCH, HAP_PERMISSIONS  # noqa: E402

logger = logging.getLogger("benchmarks.server")


class BenchSwitch(Accessory):
    """A minimal switch accessory with an On characteristic."""

    category = CATEGORY_SWITCH

    def __init__(self, driver: AccessoryDriver, display_name: str, aid: int) -> None:
        """Create a switch with the preloaded Switch service."""
        super().__init__(driver, display_name, aid=aid)
        self.service = self.add_preload_service("Switch")
        self.char_on = self.service.get_characteristic("On")


def build_bridge(
    driver: AccessoryDriver, accessory_count: int
) -> tuple[Bridge, list[dict[str, int]]]:
    """Build a bridge with the given number of switch accessories.

    Returns the bridge and a list of ``{"aid", "iid"}`` dicts pointing at
    the ``On`` characteristic of each switch.
    """
    bridge = Bridge(driver, "Bench Bridge")
    switches: list[dict[str, int]] = []
    for index in range(accessory_count):
        accessory = BenchSwitch(driver, f"Switch {index + 1}", aid=index + 2)
        bridge.add_accessory(accessory)
        on_iid = accessory.iid_manager.get_iid(accessory.char_on)
        assert on_iid is not None  # noqa: S101
        assert accessory.aid is not None  # noqa: S101
        switches.append({"aid": accessory.aid, "iid": on_iid})
    return bridge, switches


def load_identities(path: str) -> list[dict[str, str]]:
    """Load the pre-pairing identities written by the orchestrator."""
    with open(path, encoding="utf-8") as file_handle:
        return cast("list[dict[str, str]]", json.load(file_handle))


def reserve_port() -> int:
    """Reserve a loopback port for the HAP server.

    ``AccessoryDriver`` cannot express "pick an ephemeral port"
    (``State`` falls back to ``DEFAULT_PORT`` for port 0), so a port is
    reserved and immediately released here for the driver to bind. This
    also avoids collisions with real HAP-python instances using the
    default port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def dump_tracemalloc(path: str) -> None:
    """Write the top allocation sites to ``path`` as JSON."""
    snapshot = tracemalloc.take_snapshot()
    top = [
        {
            "location": f"{stat.traceback[0].filename}:{stat.traceback[0].lineno}",
            "blocks": stat.count,
            "bytes": stat.size,
        }
        for stat in snapshot.statistics("lineno")[:30]
    ]
    with open(path, encoding="utf-8", mode="w") as file_handle:
        json.dump(top, file_handle, indent=2)
    print(f"tracemalloc snapshot written to {path}", file=sys.stderr)


def main() -> None:
    """Run the benchmark accessory server until SIGTERM."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accessories", type=int, required=True)
    parser.add_argument("--clients-file", required=True)
    parser.add_argument("--tracemalloc-out", default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
    )

    if args.tracemalloc_out is not None:
        tracemalloc.start(10)

    # pyhap persists state via a 0600 tempfile that is os.replace()d into
    # place, and add_accessory() load()s it back when it exists. A state
    # file must therefore never be shared between servers running under
    # different uids (the py-spy-wrapped server may run as a different
    # user than a directly spawned one) - give every server its own
    # private directory instead.
    persist_dir = tempfile.mkdtemp(prefix="hap-bench-state-")

    driver = AccessoryDriver(
        address="127.0.0.1",
        listen_address="127.0.0.1",
        port=reserve_port(),
        persist_file=os.path.join(persist_dir, "accessory.state"),
        advertise=False,
    )

    started_building = time.perf_counter()
    bridge, switches = build_bridge(driver, args.accessories)
    build_ms = (time.perf_counter() - started_building) * 1000
    driver.add_accessory(bridge)

    identities = load_identities(args.clients_file)

    async def _start() -> None:
        """Pre-pair the controllers, start the HAP server and announce it.

        Any failure is announced as a JSON error line so the orchestrator
        can fail fast instead of waiting for a silent child.
        """
        try:
            for identity in identities:
                driver.pair(
                    identity["uuid"].encode("utf-8"),
                    bytes.fromhex(identity["public_key"]),
                    HAP_PERMISSIONS.ADMIN,
                )
            await driver.async_start()
            assert driver.http_server.server is not None  # noqa: S101
            assert driver.http_server.server.sockets is not None  # noqa: S101
            port = driver.http_server.server.sockets[0].getsockname()[1]
            announcement: dict[str, Any] = {
                "port": port,
                "pid": os.getpid(),
                "accessory_ltpk": driver.state.public_key.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw,
                ).hex(),
                "switches": switches,
                "build_ms": round(build_ms, 2),
                "accessories": args.accessories,
            }
        except Exception as error:  # noqa: BLE001
            print(json.dumps({"error": repr(error)}), flush=True)
            os._exit(1)
        print(json.dumps(announcement), flush=True)

    driver.add_job(_start())

    def _handle_terminate(signum: int, frame: Any) -> None:
        """Dump tracemalloc data (if requested) and stop the driver."""
        if args.tracemalloc_out is not None:
            dump_tracemalloc(args.tracemalloc_out)
        driver.signal_handler(signum, frame)

    signal.signal(signal.SIGTERM, _handle_terminate)
    signal.signal(signal.SIGINT, _handle_terminate)

    # Not driver.start(): it would schedule a second async_start() next to
    # the one inside _start(), and the duplicate bind on the same port
    # would fail. Run the loop directly instead.
    try:
        driver.loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        driver.loop.close()
        shutil.rmtree(persist_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
