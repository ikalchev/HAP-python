"""Tests for the HAPServer."""

import asyncio
from unittest.mock import MagicMock

import pytest

from pyhap import hap_server


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
    server.connections[client_2_addr_info] = None
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
