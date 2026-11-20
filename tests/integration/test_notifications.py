"""Integration tests for the EVENT notification paths.

A subscribed client receives an ``EVENT/1.0`` push when another client writes
the characteristic, and when the accessory changes the value itself. The
client that made the write is not sent its own update back. Events are
coalesced server-side within a 0.5 s window, so "no event" assertions wait
longer than that.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from pyhap.const import STANDALONE_AID

from .helpers import SwitchAccessory, build_switch, characteristic_value, iid_of

# Real socket + crypto round trips and the server-side 0.5 s event coalescing
# window; the CI default of --timeout=2 is too tight for these.
pytestmark = pytest.mark.timeout(10)

EVENT_WAIT = 5.0
NO_EVENT_WAIT = 1.5


async def test_write_by_one_client_is_evented_to_the_other_subscribed_client(
    make_hap_server: Any,
) -> None:
    """A write from client A reaches subscribed client B, but not A itself."""
    server = await make_hap_server(build_switch, client_count=2)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    on_iid = iid_of(accessory, accessory.char_on)
    writer = await server.connect(0)
    subscriber = await server.connect(1)

    assert (await writer.subscribe(STANDALONE_AID, on_iid)).status == 204
    assert (await subscriber.subscribe(STANDALONE_AID, on_iid)).status == 204
    writer.clear_events()
    subscriber.clear_events()

    await writer.write_characteristic(STANDALONE_AID, on_iid, True)

    # The other subscribed client receives the update...
    await subscriber.wait_event(STANDALONE_AID, on_iid, True, timeout=EVENT_WAIT)
    # ...while the writer is not sent its own write back.
    with pytest.raises(TimeoutError):
        await writer.next_event(timeout=NO_EVENT_WAIT)

    # The reverse direction works the same way.
    await subscriber.write_characteristic(STANDALONE_AID, on_iid, False)
    await writer.wait_event(STANDALONE_AID, on_iid, False, timeout=EVENT_WAIT)
    with pytest.raises(TimeoutError):
        await subscriber.next_event(timeout=NO_EVENT_WAIT)


async def test_unsubscribed_client_receives_no_events(make_hap_server: Any) -> None:
    """After ``ev: false`` the client gets no more events; others still do."""
    server = await make_hap_server(build_switch, client_count=3)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    on_iid = iid_of(accessory, accessory.char_on)
    leaver = await server.connect(0)
    stayer = await server.connect(1)
    writer = await server.connect(2)

    for client in (leaver, stayer, writer):
        assert (await client.subscribe(STANDALONE_AID, on_iid)).status == 204
    assert (await leaver.unsubscribe(STANDALONE_AID, on_iid)).status == 204
    for client in (leaver, stayer, writer):
        client.clear_events()

    await writer.write_characteristic(STANDALONE_AID, on_iid, True)

    # The still-subscribed client receives the event.
    await stayer.wait_event(STANDALONE_AID, on_iid, True, timeout=EVENT_WAIT)
    # The unsubscribed client does not, and neither does the writer itself.
    with pytest.raises(TimeoutError):
        await leaver.next_event(timeout=NO_EVENT_WAIT)
    with pytest.raises(TimeoutError):
        await writer.next_event(timeout=NO_EVENT_WAIT)


async def test_accessory_side_update_is_evented_to_subscribed_client(
    make_hap_server: Any,
) -> None:
    """A value change made by the accessory itself (no client write) is pushed."""
    server = await make_hap_server(build_switch, client_count=1)
    accessory = cast("SwitchAccessory", server.driver.accessory)
    on_iid = iid_of(accessory, accessory.char_on)
    client = await server.connect(0)

    assert (await client.subscribe(STANDALONE_AID, on_iid)).status == 204
    client.clear_events()

    # E.g. a physical switch toggled: the accessory updates its own value.
    accessory.char_on.set_value(True)

    await client.wait_event(STANDALONE_AID, on_iid, True, timeout=EVENT_WAIT)
    frame = await client.read_characteristic(STANDALONE_AID, on_iid)
    assert characteristic_value(frame, STANDALONE_AID, on_iid) is True
