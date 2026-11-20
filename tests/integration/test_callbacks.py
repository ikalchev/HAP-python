"""Integration tests for the setter/getter callbacks at all levels.

A single ``PUT /characteristics`` runs the characteristic-level setter for
each written characteristic, then the service-level setter with that
service's written characteristics (by display name), then the accessory-level
setter with ``{Service: {Characteristic: value}}``. Reads run the
characteristic-level getter.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from .helpers import LightbulbAccessory, SwitchAccessory, characteristic_value, iid_of

# Real socket + crypto round trips; the CI default of --timeout=2 is too
# tight on shared runners.
pytestmark = pytest.mark.timeout(10)


async def test_setter_and_getter_callbacks_on_all_levels(make_hap_server: Any) -> None:
    """One write triggers the char, service and accessory setters; a read the getter."""
    acc_setter_calls: list[Any] = []
    service_setter_calls: list[dict[str, Any]] = []
    char_setter_calls: list[Any] = []
    getter_calls: list[None] = []

    def build(driver: Any) -> LightbulbAccessory:
        accessory = LightbulbAccessory(driver, "Test Bulb")

        def acc_setter(updates_by_service: Any) -> None:
            acc_setter_calls.append(updates_by_service)

        def service_setter(chars: dict[str, Any]) -> None:
            service_setter_calls.append(chars)

        def char_setter(value: Any) -> None:
            char_setter_calls.append(value)

        def brightness_getter() -> int:
            getter_calls.append(None)
            return 42

        accessory.setter_callback = acc_setter
        accessory.service.setter_callback = service_setter
        accessory.char_on.setter_callback = char_setter
        accessory.char_brightness.getter_callback = brightness_getter
        return accessory

    server = await make_hap_server(build)
    accessory = cast("LightbulbAccessory", server.driver.accessory)
    aid = accessory.aid
    assert aid is not None
    on_iid = iid_of(accessory, accessory.char_on)
    brightness_iid = iid_of(accessory, accessory.char_brightness)

    client = await server.connect(0)

    # One PUT writing both characteristics triggers every setter level.
    frame = await client.write_characteristics(
        [
            {"aid": aid, "iid": on_iid, "value": True},
            {"aid": aid, "iid": brightness_iid, "value": 55},
        ]
    )
    assert frame.status == 204

    # Characteristic level: called with the raw value.
    assert char_setter_calls == [True]
    # Service level: called once with {display name: value} of its chars.
    assert service_setter_calls == [{"On": True, "Brightness": 55}]
    # Accessory level: called once with {Service: {Characteristic: value}}.
    assert len(acc_setter_calls) == 1
    updates = acc_setter_calls[0]
    assert list(updates) == [accessory.service]
    assert updates[accessory.service] == {
        accessory.char_on: True,
        accessory.char_brightness: 55,
    }

    # A read returns the getter's value for the characteristic that has one.
    frame = await client.read_characteristics([(aid, on_iid), (aid, brightness_iid)])
    assert frame.status == 200
    assert characteristic_value(frame, aid, on_iid) is True
    assert characteristic_value(frame, aid, brightness_iid) == 42
    assert len(getter_calls) == 1

    # A write of a single characteristic only reaches the levels with that char.
    char_setter_calls.clear()
    service_setter_calls.clear()
    acc_setter_calls.clear()
    frame = await client.write_characteristic(aid, on_iid, False)
    assert frame.status == 204
    assert char_setter_calls == [False]
    assert service_setter_calls == [{"On": False}]
    assert acc_setter_calls[0][accessory.service] == {accessory.char_on: False}


async def test_async_characteristic_setter_delays_the_write_response(
    make_hap_server: Any,
) -> None:
    """A coroutine characteristic setter is awaited before the write is answered."""
    setter_started = asyncio.Event()
    release_setter = asyncio.Event()
    seen: list[Any] = []

    def build(driver: Any) -> SwitchAccessory:
        accessory = SwitchAccessory(driver, "Async Switch")

        async def char_setter(value: Any) -> None:
            seen.append(value)
            setter_started.set()
            await release_setter.wait()

        accessory.char_on.setter_callback = char_setter
        return accessory

    server = await make_hap_server(build)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    aid = accessory.aid
    assert aid is not None
    on_iid = iid_of(accessory, accessory.char_on)
    client = await server.connect(0)

    write_task = asyncio.create_task(client.write_characteristic(aid, on_iid, True))
    await asyncio.wait_for(setter_started.wait(), timeout=5.0)
    assert seen == [True]
    # The setter is still running, so the write has not been answered yet.
    assert not write_task.done()
    release_setter.set()
    frame = await asyncio.wait_for(write_task, timeout=5.0)
    assert frame.status == 204

    frame = await client.read_characteristic(aid, on_iid)
    assert characteristic_value(frame, aid, on_iid) is True
