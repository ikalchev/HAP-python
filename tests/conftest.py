"""Test fictures and mocks."""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pyhap.accessory_driver import AccessoryDriver
from pyhap.loader import Loader


@pytest.fixture(scope="session")
def mock_driver() -> Iterator["MockDriver"]:
    yield MockDriver()


@pytest.fixture(name="async_zeroconf")
def async_zc() -> Iterator[MagicMock]:
    with patch("pyhap.accessory_driver.AsyncZeroconf") as mock_async_zeroconf:
        aiozc = mock_async_zeroconf.return_value
        aiozc.async_register_service = AsyncMock()
        aiozc.async_update_service = AsyncMock()
        aiozc.async_unregister_service = AsyncMock()
        aiozc.async_close = AsyncMock()
        yield aiozc


@pytest.fixture
def driver(async_zeroconf: Any) -> Iterator[AccessoryDriver]:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
    ):
        yield AccessoryDriver(loop=loop)


@pytest.fixture(autouse=True)
def mock_local_address() -> Iterator[None]:
    with patch("pyhap.util.get_local_address", return_value="127.0.0.1"):
        yield


class MockDriver:
    def __init__(self) -> None:
        self.loader = Loader()
        self.loop = None

    def publish(
        self, data: Any, client_addr: Any = None, immediate: bool = False
    ) -> None:
        pass

    def add_job(self, target: Callable[..., Any], *args: Any) -> None:
        asyncio.new_event_loop().run_until_complete(target(*args))
