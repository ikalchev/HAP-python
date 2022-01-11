"""Tests for the HAPServer."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pyhap import hap_server
from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.hap_protocol import HAPServerProtocol


@pytest.mark.asyncio
async def test_we_can_start_stop(driver):
    """Test we can start and stop."""
    loop = asyncio.get_event_loop()
    addr_info = ("0.0.0.0", None)
    client_1_addr_info = ("1.2.3.4", 44433)
    client_2_addr_info = ("4.5.6.7", 33444)

    server = hap_server.HAPServer(addr_info, driver)
    await server.async_start(loop)
    server.connections[client_1_addr_info] = MagicMock()
    server.connections[client_2_addr_info] = MagicMock()
    server.async_stop()


@pytest.mark.asyncio
async def test_we_can_connect():
    """Test we can start, connect, and stop."""
    loop = asyncio.get_event_loop()
    with patch("pyhap.accessory_driver.AsyncZeroconf"), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ):
        driver = AccessoryDriver(loop=loop)

    driver.add_accessory(Accessory(driver, "TestAcc"))

    addr_info = ("0.0.0.0", None)
    server = hap_server.HAPServer(addr_info, driver)
    await server.async_start(loop)
    sock = server.server.sockets[0]
    assert not server.connections
    _, port = sock.getsockname()
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    # flush out any call_soon
    for _ in range(3):
        await asyncio.sleep(0)
    assert server.connections
    server.async_stop()
    writer.close()


@pytest.mark.asyncio
async def test_idle_connection_cleanup():
    """Test we cleanup idle connections."""
    loop = asyncio.get_event_loop()
    addr_info = ("0.0.0.0", None)
    client_1_addr_info = ("1.2.3.4", 44433)

    with patch.object(hap_server, "IDLE_CONNECTION_CHECK_INTERVAL_SECONDS", 0), patch(
        "pyhap.accessory_driver.AsyncZeroconf"
    ), patch("pyhap.accessory_driver.AccessoryDriver.persist"), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
    ):
        driver = AccessoryDriver(loop=loop)
        server = hap_server.HAPServer(addr_info, driver)
        await server.async_start(loop)
        check_idle = MagicMock()
        server.connections[client_1_addr_info] = MagicMock(check_idle=check_idle)
        for _ in range(3):
            await asyncio.sleep(0)
        assert check_idle.called
        check_idle.reset_mock()
        for _ in range(3):
            await asyncio.sleep(0)
        assert check_idle.called
    server.async_stop()


@pytest.mark.asyncio
async def test_push_event(driver):
    """Test we can create and send an event."""
    addr_info = ("1.2.3.4", 1234)
    server = hap_server.HAPServer(("127.0.01", 5555), driver)
    server.loop = asyncio.get_event_loop()
    hap_events = []

    def _save_event(hap_event):
        hap_events.append(hap_event)

    hap_server_protocol = HAPServerProtocol(
        server.loop, server.connections, server.accessory_handler
    )
    hap_server_protocol.write = _save_event
    hap_server_protocol.peername = addr_info
    server.accessory_handler.topics["1.33"] = {addr_info}
    server.accessory_handler.topics["2.33"] = {addr_info}
    server.accessory_handler.topics["3.33"] = {addr_info}

    assert server.push_event({"aid": 1, "iid": 33, "value": False}, addr_info) is False
    await asyncio.sleep(0)
    server.connections[addr_info] = hap_server_protocol

    assert (
        server.push_event({"aid": 1, "iid": 33, "value": False}, addr_info, True)
        is True
    )
    assert (
        server.push_event({"aid": 2, "iid": 33, "value": False}, addr_info, True)
        is True
    )
    assert (
        server.push_event({"aid": 3, "iid": 33, "value": False}, addr_info, True)
        is True
    )

    await asyncio.sleep(0)
    assert hap_events == [
        b"EVENT/1.0 200 OK\r\nContent-Type: application/hap+json\r\nContent-Length: 120\r\n\r\n"
        b'{"characteristics":[{"aid":1,"iid":33,"value":false},'
        b'{"aid":2,"iid":33,"value":false},{"aid":3,"iid":33,"value":false}]}'
    ]

    hap_events = []
    assert (
        server.push_event({"aid": 1, "iid": 33, "value": False}, addr_info, False)
        is True
    )
    assert (
        server.push_event({"aid": 2, "iid": 33, "value": False}, addr_info, False)
        is True
    )
    assert (
        server.push_event({"aid": 3, "iid": 33, "value": False}, addr_info, False)
        is True
    )

    await asyncio.sleep(0)
    assert not hap_events

    # Ensure that a the event is not sent if its unsubscribed during
    # the coalesce delay
    server.accessory_handler.topics["1.33"].remove(addr_info)

    await asyncio.sleep(0.55)
    assert hap_events == [
        b"EVENT/1.0 200 OK\r\nContent-Type: application/hap+json\r\nContent-Length: 87\r\n\r\n"
        b'{"characteristics":[{"aid":2,"iid":33,"value":false},{"aid":3,"iid":33,"value":false}]}'
    ]


@pytest.mark.asyncio
async def test_push_event_overwrites_old_pending_events(driver):
    """Test push event overwrites old events in the event queue.

    iOS 15 had a breaking change where events are no longer processed
    in order. We want to make sure when we send an event message we
    only send the latest state and overwrite all the previous states
    for the same AID/IID that are in the queue when the state changes
    before the event is sent.
    """
    addr_info = ("1.2.3.4", 1234)
    server = hap_server.HAPServer(("127.0.01", 5555), driver)
    server.loop = asyncio.get_event_loop()
    hap_events = []

    def _save_event(hap_event):
        hap_events.append(hap_event)

    hap_server_protocol = HAPServerProtocol(
        server.loop, server.connections, server.accessory_handler
    )
    hap_server_protocol.write = _save_event
    hap_server_protocol.peername = addr_info
    server.accessory_handler.topics["1.33"] = {addr_info}
    server.accessory_handler.topics["2.33"] = {addr_info}
    server.connections[addr_info] = hap_server_protocol

    assert (
        server.push_event({"aid": 1, "iid": 33, "value": False}, addr_info, True)
        is True
    )
    assert (
        server.push_event({"aid": 1, "iid": 33, "value": True}, addr_info, True) is True
    )
    assert (
        server.push_event({"aid": 2, "iid": 33, "value": False}, addr_info, True)
        is True
    )

    await asyncio.sleep(0)
    assert hap_events == [
        b"EVENT/1.0 200 OK\r\nContent-Type: application/hap+json\r\nContent-Length: 86\r\n\r\n"
        b'{"characteristics":[{"aid":1,"iid":33,"value":true},'
        b'{"aid":2,"iid":33,"value":false}]}'
    ]
