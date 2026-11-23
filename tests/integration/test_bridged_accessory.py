"""Integration tests for a Bridge with bridged accessories over real HAP."""

from __future__ import annotations

from typing import Any, cast

import pytest

from pyhap.accessory import Bridge

from .helpers import (
    SwitchAccessory,
    body_json,
    characteristic_value,
    iid_of,
)

# Real socket + crypto round trips; the CI default of --timeout=2 is too
# tight on shared runners.
pytestmark = pytest.mark.timeout(10)


def build_bridge(driver: Any) -> Bridge:
    """A bridge with two switch accessories."""
    bridge = Bridge(driver, "Test Bridge")
    bridge.add_accessory(SwitchAccessory(driver, "Switch One", aid=2))
    bridge.add_accessory(SwitchAccessory(driver, "Switch Two", aid=3))
    return bridge


async def test_bridge_serves_all_bridged_accessories(make_hap_server: Any) -> None:
    """GET /accessories lists the bridge and every bridged accessory."""
    server = await make_hap_server(build_bridge)
    client = await server.connect(0)

    frame = await client.get_accessories()
    assert frame.status == 200
    accessories = body_json(frame)["accessories"]
    assert [acc["aid"] for acc in accessories] == [1, 2, 3]
    # The bridge's own accessory information carries the bridge name.
    bridge = cast("Bridge", server.driver.accessory)
    info = bridge.get_service("AccessoryInformation")
    assert info is not None
    name_iid = iid_of(bridge, info.get_characteristic("Name"))
    frame = await client.read_characteristic(1, name_iid)
    assert characteristic_value(frame, 1, name_iid) == "Test Bridge"


async def test_bridged_characteristics_are_addressed_by_aid(
    make_hap_server: Any,
) -> None:
    """Reads and writes on one bridged accessory leave the others alone."""
    server = await make_hap_server(build_bridge)
    bridge = cast("Bridge", server.driver.accessory)
    acc_one = cast("SwitchAccessory", bridge.accessories[2])
    acc_two = cast("SwitchAccessory", bridge.accessories[3])
    on_one = iid_of(acc_one, acc_one.char_on)
    on_two = iid_of(acc_two, acc_two.char_on)

    client = await server.connect(0)

    # One request can read characteristics of several accessories.
    frame = await client.read_characteristics([(2, on_one), (3, on_two)])
    assert frame.status == 200
    assert characteristic_value(frame, 2, on_one) is False
    assert characteristic_value(frame, 3, on_two) is False

    # Writing aid 2 does not touch aid 3.
    frame = await client.write_characteristic(2, on_one, True)
    assert frame.status == 204
    frame = await client.read_characteristics([(2, on_one), (3, on_two)])
    assert characteristic_value(frame, 2, on_one) is True
    assert characteristic_value(frame, 3, on_two) is False

    # And the other way around.
    frame = await client.write_characteristic(3, on_two, True)
    assert frame.status == 204
    frame = await client.read_characteristics([(2, on_one), (3, on_two)])
    assert characteristic_value(frame, 2, on_one) is True
    assert characteristic_value(frame, 3, on_two) is True
