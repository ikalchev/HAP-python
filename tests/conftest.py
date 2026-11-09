"""Test fictures and mocks."""

import asyncio
from collections.abc import Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from pyhap import hksv3
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera_stream import StreamController, StreamSession
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


STREAM_SENSOR = hksv3.SensorConfiguration(
    UUID("00112233-4455-6677-8899-aabbccddeeff"),
    3840,
    2160,
    video_streams=[
        hksv3.VideoStreamCapability(UUID(int=1), 2, 1920, 1080, 30, 1700, 1800),
        hksv3.VideoStreamCapability(UUID(int=2), 3, 1280, 720, 30, 768, 800),
        hksv3.VideoStreamCapability(UUID(int=3), 4, 640, 360, 15, 180, 190),
    ],
)


@pytest.fixture
def stream_sensor() -> hksv3.SensorConfiguration:
    """A 4K sensor with High, Medium and Low encodings."""
    return STREAM_SENSOR


class _StubStreamController(StreamController[StreamSession]):
    """A controller whose delegate only releases sessions."""

    def __init__(self, sensor: hksv3.SensorConfiguration, release: AsyncMock) -> None:
        camera = MagicMock()
        camera.driver.loop = asyncio.get_running_loop()
        super().__init__(camera, sensor, 0.05, 5, 3)
        self.release = release

    async def _release_hook(self, session: StreamSession) -> None:
        await self.release(session)


@pytest.fixture
def session_release() -> AsyncMock:
    """The release hook of ``session_controller``."""
    return AsyncMock()


@pytest.fixture
async def session_controller(
    stream_sensor: hksv3.SensorConfiguration, session_release: AsyncMock
) -> StreamController[StreamSession]:
    """A stream controller with a hook timeout of 50 ms and 5 sessions, 3 each."""
    return _StubStreamController(stream_sensor, session_release)


@pytest.fixture
def add_session(
    session_controller: StreamController[StreamSession],
) -> Callable[[Any], StreamSession]:
    """Add a new session of a connection to ``session_controller``."""

    def _add(client_addr: Any) -> StreamSession:
        session = StreamSession(
            id=uuid4(), sensor=session_controller.sensor, client_addr=client_addr
        )
        session_controller._add(session)
        return session

    return _add
