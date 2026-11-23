"""Toggle the example Lightbulb accessory with a pyhap HAP client.

Start ``lightbulb_accessory.py`` first, then run this script. The controller
identity in ``client.state`` (created once with
``python -m pyhap.client new -f client.state``) is paired by the accessory on
startup; the accessory's LTPK is pinned from ``accessory.state`` on first use.
See README.md in this directory for the two-machine variant.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from pyhap.client import HAPClient

HERE = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 51827

LIGHTBULB_SERVICE_TYPE = "43"  # 00000043-0000-1000-8000-0026BB765291, short HAP form
ON_CHARACTERISTIC_TYPE = "25"  # 00000025-0000-1000-8000-0026BB765291, short HAP form


def find_on_characteristic(accessories: Any) -> tuple[int, int] | None:
    """The (aid, iid) of the On characteristic of the first Lightbulb service."""
    for acc in accessories:
        for service in acc["services"]:
            if service["type"] != LIGHTBULB_SERVICE_TYPE:
                continue
            for char in service["characteristics"]:
                if char["type"] == ON_CHARACTERISTIC_TYPE:
                    return acc["aid"], char["iid"]
    return None


async def main() -> int:
    """Connect, find the lightbulb's On characteristic, toggle it, read back."""
    client = HAPClient.new_persistent(
        filepath=HERE / "client.state",
        accessory_state=HERE / "accessory.state",  # pins the accessory LTPK once
    )
    await client.connect(HOST, PORT)
    try:
        frame = await client.get_accessories()
        on = find_on_characteristic(json.loads(frame.body)["accessories"])
        if on is None:
            print("no Lightbulb with an On characteristic found", file=sys.stderr)
            return 1
        aid, iid = on
        # EVENT pushes arrive here when the value changes from another
        # controller or from the accessory itself (not for our own writes).
        await client.subscribe(aid, iid)

        frame = await client.read_characteristic(aid, iid)
        current = json.loads(frame.body)["characteristics"][0]["value"]
        await client.write_characteristic(aid, iid, not current)

        frame = await client.read_characteristic(aid, iid)
        value = json.loads(frame.body)["characteristics"][0]["value"]
        print(f"Lightbulb On (aid={aid}, iid={iid}) is now {'on' if value else 'off'}")
    finally:
        await client.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
