"""Example accessory: a Lightbulb that can be toggled with a pyhap HAP client.

Run this, then ``control_lightbulb.py``. If ``client.state`` exists next to
this file (create it once with ``python -m pyhap.client new -f client.state``),
its controller identity is paired automatically on every start (pairing is
idempotent). Set ``advertise=True`` to also show the accessory in the Home app.
"""

from __future__ import annotations

from pathlib import Path
import signal

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.client import ClientIdentity
from pyhap.const import CATEGORY_LIGHTBULB, HAP_PERMISSIONS
from pyhap.util import get_local_address

HERE = Path(__file__).resolve().parent
CLIENT_STATE = HERE / "client.state"


class LightBulb(Accessory):
    """A lightbulb whose On characteristic prints every change."""

    category = CATEGORY_LIGHTBULB

    def __init__(
        self, driver: AccessoryDriver, display_name: str, aid: int | None = None
    ) -> None:
        """Create the lightbulb with the preloaded Lightbulb service."""
        super().__init__(driver, display_name, aid=aid)
        service = self.add_preload_service("Lightbulb")
        self.char_on = service.configure_char("On", setter_callback=self.bulb_toggled)

    def bulb_toggled(self, value: bool) -> None:
        """Called on every write to On from a controller."""
        print(f"The light is now {'on' if value else 'off'}", flush=True)

    async def run(self) -> None:
        """Pair the client identity from client.state, if present (idempotent)."""
        if CLIENT_STATE.exists():
            identity = ClientIdentity.load(CLIENT_STATE)
            self.driver.pair(
                identity.uuid_bytes,
                identity.public_key_bytes,
                HAP_PERMISSIONS.ADMIN,
            )
            print(f"Paired HAP controller {identity.uuid}", flush=True)


def main() -> None:
    """Run the accessory until Ctrl+C / SIGTERM."""
    driver = AccessoryDriver(
        port=51827,
        advertise=False,  # no mDNS for this demo; True to appear in the Home app
        # Listen on loopback (client on the same machine) and the LAN address
        # (client on another machine, see README.md).
        listen_address=["127.0.0.1", get_local_address()],
        persist_file=str(HERE / "accessory.state"),
    )
    driver.add_accessory(LightBulb(driver, "Example LightBulb"))
    signal.signal(signal.SIGTERM, driver.signal_handler)
    driver.start()


if __name__ == "__main__":
    main()
