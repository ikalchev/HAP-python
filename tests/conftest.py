"""Test fictures and mocks."""

import asyncio
from unittest.mock import patch

import pytest

from pyhap.accessory_driver import AccessoryDriver
from pyhap.loader import Loader


@pytest.fixture(scope="session")
def mock_driver():
    yield MockDriver()


@pytest.fixture
def driver():
    with patch("pyhap.accessory_driver.HAPServer"), patch(
        "pyhap.accessory_driver.Zeroconf"
    ), patch("pyhap.accessory_driver.AccessoryDriver.persist"):
        yield AccessoryDriver()


class MockDriver:
    def __init__(self):
        self.loader = Loader()

    def publish(self, data, client_addr=None):
        pass

    def add_job(self, target, *args):  # pylint: disable=no-self-use
        asyncio.get_event_loop().run_until_complete(target(*args))
