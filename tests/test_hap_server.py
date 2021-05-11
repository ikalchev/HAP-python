"""Tests for the HAPServer."""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from pyhap import hap_server
from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver


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
    assert server.connections == {}
    _, port = sock.getsockname()
    _, writer = await asyncio.open_connection("127.0.0.1", port)
    # flush out any call_soon
    for _ in range(3):
        await asyncio.sleep(0)
    assert server.connections != {}
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


def test_push_event(driver):
    """Test we can create and send an event."""
    addr_info = ("1.2.3.4", 1234)
    server = hap_server.HAPServer(addr_info, driver)
    hap_events = []

    def _save_event(hap_event):
        hap_events.append(hap_event)

    hap_server_protocol = MagicMock()
    hap_server_protocol.write = _save_event

    assert server.push_event(b"data", addr_info) is False
    server.connections[addr_info] = hap_server_protocol

    assert server.push_event(b"data", addr_info) is True
    assert hap_events == [
        b"EVENT/1.0 200 OK\r\nContent-Type: application/hap+json\r\nContent-Length: 4\r\n\r\ndata"
    ]
