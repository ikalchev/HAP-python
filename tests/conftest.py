"""Test fictures and mocks."""

import asyncio
from unittest.mock import patch

import pytest

from pyhap.accessory_driver import AccessoryDriver
from pyhap.loader import Loader

from . import AsyncMock


@pytest.fixture(scope="session")
def mock_driver():
    yield MockDriver()


@pytest.fixture(name="async_zeroconf")
def async_zc():
    with patch("pyhap.accessory_driver.AsyncZeroconf") as mock_async_zeroconf:
        aiozc = mock_async_zeroconf.return_value
        aiozc.async_register_service = AsyncMock()
        aiozc.async_update_service = AsyncMock()
        aiozc.async_unregister_service = AsyncMock()
        aiozc.async_close = AsyncMock()
        yield aiozc


@pytest.fixture
def driver(async_zeroconf):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ):
        yield AccessoryDriver(loop=loop)


@pytest.fixture(autouse=True)
def mock_local_address():
    with patch("pyhap.util.get_local_address", return_value="127.0.0.1"):
        yield


class MockDriver:
    def __init__(self):
        self.loader = Loader()

    def publish(self, data, client_addr=None, immediate=False):
        pass

    def add_job(self, target, *args):
        asyncio.new_event_loop().run_until_complete(target(*args))
