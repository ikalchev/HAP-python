"""A lightbulb driven entirely by delegates and their contexts.

The modern counterpart of ``busy_lock.py``: instead of legacy
``setter_callback``/``getter_callback`` attributes, one
:class:`~pyhap.delegates.CharacteristicDelegate` serves the bulb's
characteristics — the context of each call says which one — and a
:class:`~pyhap.delegates.ServiceDelegate` is the pre-check of the service's
grouped writes. Every delegate method is a coroutine function that may do
IO under the request deadline, and a refusal is a
:class:`~pyhap.errors.HapStatusError`, not the generic ``-70402``.
"""

import asyncio
from collections.abc import Mapping
import logging
import signal
import time
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.const import CATEGORY_LIGHTBULB, HAP_SERVER_STATUS
from pyhap.context import CharReadContext, CharWriteContext, ServiceWriteContext
from pyhap.delegates import CharacteristicDelegate, ServiceDelegate
from pyhap.errors import HapStatusError

logging.basicConfig(level=logging.INFO, format="[%(module)s] %(message)s")


class BulbDelegate(CharacteristicDelegate):
    """One delegate for every characteristic of the bulb.

    The context carries ``characteristic``, so a single instance can serve
    them all. The bulb here is simulated: applying a command takes
    ``COMMAND_SECONDS``, and a command that arrives while the bulb is still
    applying the previous one is refused with RESOURCE_BUSY.
    """

    COMMAND_SECONDS = 0.5
    READ_SECONDS = 0.05

    def __init__(self) -> None:
        # What the bulb itself reports, by characteristic display name.
        self._state: dict[str, Any] = {"On": False, "Brightness": 100}
        self._busy_until = 0.0

    async def on_get(self, context: CharReadContext) -> Any:
        # Reading the bulb is IO, and a delegate's read may await it: the
        # driver runs on_get under the request deadline, off the sync path.
        await asyncio.sleep(self.READ_SECONDS)
        return self._state[context.characteristic.display_name]

    async def on_set(self, value: Any, context: CharWriteContext) -> Any:
        char = context.characteristic
        if time.monotonic() < self._busy_until:
            # The bulb is still applying the previous command: refuse this
            # one with a status the Home app can show, the driver restores
            # the previous value and notifies nobody.
            logging.info("Refusing %s = %s, the bulb is busy", char.display_name, value)
            raise HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)
        self._busy_until = time.monotonic() + self.COMMAND_SECONDS
        logging.info(
            "%s (admin: %s) sets %s to %s",
            context.client_addr,
            context.is_admin,
            char.display_name,
            value,
        )
        await asyncio.sleep(self.COMMAND_SECONDS)
        self._state[char.display_name] = value


class BulbGate(ServiceDelegate):
    """The pre-check of the bulb service's grouped writes.

    A request that writes several characteristics of the service runs this
    first, with every written value: turning the light off while also
    setting a brightness makes no sense, so the whole group is refused —
    unless the writer is an admin.
    """

    async def on_set(
        self, values: Mapping[Characteristic, Any], context: ServiceWriteContext
    ) -> None:
        service = context.service
        on_char = service.get_characteristic("On")
        brightness_char = service.get_characteristic("Brightness")
        turns_off = on_char in values and not values[on_char]
        if turns_off and brightness_char in values and not context.is_admin:
            logging.info("Refusing to set a brightness while turning the light off")
            raise HapStatusError(HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST)


class DelegateLightbulb(Accessory):
    """A light with On and Brightness, wired to delegates only."""

    category = CATEGORY_LIGHTBULB

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)

        service = self.add_preload_service("Lightbulb", ["Brightness"])
        self.bulb = BulbDelegate()
        service.delegate = BulbGate()
        service.configure_char("On", delegate=self.bulb)
        service.configure_char("Brightness", delegate=self.bulb)
        self.set_info_service(serial_number="delegate lightbulb")


driver = AccessoryDriver(port=51826, persist_file="delegate_lightbulb.state")
driver.add_accessory(accessory=DelegateLightbulb(driver, "Delegate Lightbulb"))
signal.signal(signal.SIGTERM, driver.signal_handler)
driver.start()
