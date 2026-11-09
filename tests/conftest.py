"""Test fictures and mocks."""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from pyhap import hksv3
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import Camera
from pyhap.camera_multi_tier import MultiTierDelegate
from pyhap.camera_stream import StreamController, StreamSession
from pyhap.camera_webrtc import WebRTCDelegate
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
STREAM_CAMERA_AID = 2
# The classic RTP stream management service the 17.99 services need.
STREAM_VIDEO = {
    "codec": {"profiles": [b"\x01"], "levels": [b"\x02"]},
    "resolutions": [[640, 360, 30]],
}
STREAM_AUDIO = {"codecs": [{"type": "OPUS", "samplerate": 24}]}


@pytest.fixture(name="stream_driver")
async def stream_driver_fixture(
    tmp_path: pathlib.Path,
) -> AsyncIterator[AccessoryDriver]:
    """A driver on the running loop for HKSV 17.99 stream tests."""
    with patch("pyhap.accessory_driver.AccessoryDriver.async_persist"):
        yield AccessoryDriver(
            loop=asyncio.get_running_loop(),
            listen_address="127.0.0.1",
            persist_file=str(tmp_path / "accessory.state"),
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


@pytest.fixture
def multi_tier_delegate() -> AsyncMock:
    """A multi-tier delegate whose hooks succeed."""
    return AsyncMock(spec=MultiTierDelegate)


@pytest.fixture
def webrtc_delegate() -> AsyncMock:
    """A WebRTC delegate that offers with SFrame and answers reoffers."""
    delegate = AsyncMock(spec=WebRTCDelegate)
    delegate.handle_solicit_offer.return_value = hksv3.WebRTCOffer(
        "v=0\r\n", hksv3.SFrameKey(bytes(32), 7)
    )
    delegate.handle_reoffer.return_value = hksv3.WebRTCReofferAnswer(
        "v=0\r\na=answer\r\n"
    )
    return delegate


@pytest.fixture
def stream_camera(
    stream_driver: AccessoryDriver,
    multi_tier_delegate: AsyncMock,
    webrtc_delegate: AsyncMock,
) -> Callable[..., Camera]:
    """Build a camera with the multi-tier and WebRTC services and delegates."""

    def _make(**options: Any) -> Camera:
        return Camera(
            {
                "address": "127.0.0.1",
                "video": STREAM_VIDEO,
                "audio": STREAM_AUDIO,
                "sensors": [STREAM_SENSOR],
                "global_operating_mode": ["ManuallyDisabled"],
                "recording": True,
                "multi_tier": {"delegate": multi_tier_delegate},
                "webrtc": {"delegate": webrtc_delegate},
                **options,
            },
            stream_driver,
            "Cam",
            aid=STREAM_CAMERA_AID,
        )

    return _make
