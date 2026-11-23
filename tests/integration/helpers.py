"""Shared accessories and response helpers for the integration tests."""

from __future__ import annotations

import json
from typing import Any

from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import Characteristic
from pyhap.client import Frame
from pyhap.const import CATEGORY_LIGHTBULB, CATEGORY_SWITCH

# Service / characteristic type UUIDs in the short HAP form used in JSON
# payloads.
SWITCH_SERVICE_TYPE = "49"
ON_CHARACTERISTIC_TYPE = "25"
ACCESSORY_INFORMATION_TYPE = "3E"
LIGHTBULB_SERVICE_TYPE = "43"


class SwitchAccessory(Accessory):
    """A switch accessory with a single writable ``On`` characteristic."""

    category = CATEGORY_SWITCH

    def __init__(
        self, driver: AccessoryDriver, display_name: str, aid: int | None = None
    ) -> None:
        """Create the switch with the preloaded Switch service."""
        super().__init__(driver, display_name, aid=aid)
        self.service = self.add_preload_service("Switch")
        self.char_on = self.service.get_characteristic("On")


class LightbulbAccessory(Accessory):
    """A lightbulb with writable ``On`` and ``Brightness`` characteristics."""

    category = CATEGORY_LIGHTBULB

    def __init__(
        self, driver: AccessoryDriver, display_name: str, aid: int | None = None
    ) -> None:
        """Create the lightbulb with the preloaded Lightbulb service."""
        super().__init__(driver, display_name, aid=aid)
        self.service = self.add_preload_service("Lightbulb", chars=["Brightness"])
        self.char_on = self.service.get_characteristic("On")
        self.char_brightness = self.service.get_characteristic("Brightness")


def build_switch(driver: AccessoryDriver) -> SwitchAccessory:
    """The switch accessory the integration tests run against."""
    return SwitchAccessory(driver, "Test Switch")


def iid_of(accessory: Accessory, char: Characteristic) -> int:
    """The IID the driver assigned to ``char`` within ``accessory``."""
    iid = accessory.iid_manager.get_iid(char)
    assert iid is not None
    return iid


def body_json(frame: Frame) -> Any:
    """Parse the JSON body of a response frame."""
    return json.loads(frame.body)


def characteristic_entries(frame: Frame) -> dict[tuple[int, int], dict[str, Any]]:
    """The ``characteristics`` list of a response, keyed by ``(aid, iid)``."""
    return {
        (int(entry["aid"]), int(entry["iid"])): entry
        for entry in body_json(frame)["characteristics"]
    }


def characteristic_value(frame: Frame, aid: int, iid: int) -> Any:
    """The ``value`` of one characteristic in a read response."""
    return characteristic_entries(frame)[(aid, iid)]["value"]
