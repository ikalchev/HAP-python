"""Tests for the HKSV 17.99 multi-tier RTP streaming of pyhap.camera."""

import asyncio
from collections.abc import Callable
from typing import Any, cast
from unittest.mock import patch
from uuid import UUID

import pytest

from pyhap import hksv3
from pyhap.camera import Camera
from pyhap.camera_multi_tier import (
    MultiTierController,
)
from pyhap.camera_webrtc import WebRTCController
from pyhap.util import base64_to_bytes, to_base64_str

MULTI_TIER = "CameraMultiTierRTPStreamManagement"
WEBRTC = "CameraWebRTCStreamManagement"
SENSOR = UUID("00112233-4455-6677-8899-aabbccddeeff")
SESSION = UUID("acccc6cc-1505-6345-158d-a974b4509006")
CLIENT = ("192.168.1.114", 50000)
OTHER = ("192.168.1.115", 50000)
KEY = bytes(range(16))
SALT = bytes(range(14))
HIGH = hksv3.VideoStreamTier(1, 2, 1700, 1920, 1080, 30, 1800)
MEDIUM = hksv3.VideoStreamTier(2, 3, 768, 1280, 720, 30, 800)


def _controller(camera: Camera) -> MultiTierController:
    return camera.multi_tier_controllers[SENSOR]


def _char(camera: Camera, service: str, name: str) -> Any:
    found = camera.get_service(service)
    assert found is not None
    return found.get_characteristic(name)


# Services and options


async def test_services(stream_camera: Callable[..., Camera]) -> None:
    """Test the advertised tiers, payload types and linked services."""
    camera = stream_camera()
    multi_tier = camera.get_service(MULTI_TIER)
    webrtc = camera.get_service(WEBRTC)
    transport = camera.get_service("DataStreamTransportManagement")
    assert multi_tier is not None
    assert webrtc is not None
    video = base64_to_bytes(
        _char(camera, MULTI_TIER, "SupportedVideoStreamTiers").value
    )
    assert video[:6] == bytes.fromhex("010102 020163")
    assert (
        video
        == hksv3.SupportedVideoStreamTiers(
            hksv3.VideoCodec.H265, 99, _controller(camera).tiers
        ).encode()
    )
    audio = base64_to_bytes(
        _char(camera, MULTI_TIER, "SupportedAudioStreamTiers").value
    )
    assert audio == bytes.fromhex(
        "010103 02016e 0318 010401000000 0204c05d0000 030104 040102 050114 060101"
    )
    assert _char(camera, WEBRTC, "WebRTCSupportedVideoStreamTiers").value == (
        to_base64_str(video)
    )
    assert _char(camera, WEBRTC, "WebRTCSupportedAudioStreamTiers").value == (
        to_base64_str(audio)
    )
    assert base64_to_bytes(_char(camera, WEBRTC, "SensorUUID").value) == SENSOR.bytes
    assert _char(camera, MULTI_TIER, "StatusActive").value is True
    assert multi_tier.linked_services == [webrtc, transport]
    assert webrtc.linked_services == [multi_tier, transport]
    assert camera.services.index(multi_tier) < camera.services.index(webrtc)


async def test_rtp_services_come_after(stream_camera: Callable[..., Camera]) -> None:
    """Test the RTP stream management services follow with a new unique_id."""
    camera = stream_camera()
    rtp = camera.get_service("CameraRTPStreamManagement")
    multi_tier = camera.get_service(MULTI_TIER)
    assert rtp is not None
    assert rtp.unique_id == "rtp-0"
    assert camera.services.index(cast(Any, multi_tier)) < camera.services.index(rtp)


@pytest.mark.parametrize(
    ("options", "match"),
    [
        ({"webrtc": None}, "need each other"),
        ({"multi_tier": None}, "need each other"),
        ({"video": None}, "need the video and audio"),
        ({"video": {}}, "need the video and audio"),
        ({"audio": None}, "need the video and audio"),
        ({"audio": {}}, "need the video and audio"),
        ({"stream_count": 0}, "need the video and audio"),
        ({"sensors": []}, "need the sensors"),
        ({"global_operating_mode": None}, "need the sensors"),
        ({"recording": False}, "need the sensors"),
        ({"multi_tier": {"start_stream_cmd": "ffmpeg"}}, "Unknown multi_tier"),
        ({"webrtc": {"offer_timeout": 8}}, "Unknown webrtc option"),
        ({"multi_tier": {"max_sessions": 4}}, "at least 5"),
        ({"webrtc": {"max_sessions": 5}}, "at least 6"),
        ({"multi_tier": {"max_sessions_per_connection": 0}}, "must be 1 to"),
        ({"webrtc": {"max_sessions_per_connection": 7}}, "must be 1 to"),
        ({"multi_tier": {"hook_timeout": 9}}, "hook_timeout"),
        ({"webrtc": {"remote_quality": 5}}, "remote_quality"),
        ({"multi_tier": {"audio": None}}, "exactly one audio tier"),
    ],
)
async def test_invalid_options(
    stream_camera: Callable[..., Camera], options: dict[str, Any], match: str
) -> None:
    """Test invalid combinations and values raise."""
    with pytest.raises(ValueError, match=match):
        stream_camera(**options)


async def test_tier_warnings(
    stream_camera: Callable[..., Camera], caplog: pytest.LogCaptureFixture
) -> None:
    """Test bit rates beyond the guide are logged, not refused."""
    sensor = hksv3.SensorConfiguration(
        SENSOR,
        1920,
        1080,
        video_streams=[
            hksv3.VideoStreamCapability(UUID(int=1), 2, 1920, 1080, 30, 1700, 2500),
            hksv3.VideoStreamCapability(UUID(int=2), 3, 1280, 720, 30, 768, 800),
            hksv3.VideoStreamCapability(UUID(int=3), 4, 640, 360, 15, 180, 190),
        ],
    )
    stream_camera(sensors=[sensor])
    assert "1920x1080@30: peak above the guide's 1800 kbps" in caplog.text


async def test_status_active(stream_camera: Callable[..., Camera]) -> None:
    """Test Status Active follows the global operating mode and the service."""
    camera = stream_camera()
    camera.set_manually_disabled(True)
    assert _char(camera, MULTI_TIER, "StatusActive").value is False
    camera.set_manually_disabled(False)
    _char(camera, MULTI_TIER, "StreamingEnabled").client_update_value(False)
    assert _char(camera, MULTI_TIER, "StatusActive").value is True
    await asyncio.sleep(0)
    assert _char(camera, MULTI_TIER, "StatusActive").value is False


async def test_streaming_enabled_persisted(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test the services' Streaming Enabled survive a restart."""
    camera = stream_camera()
    _char(camera, WEBRTC, "StreamingEnabled").client_update_value(False)
    restored = stream_camera()
    assert _char(restored, WEBRTC, "StreamingEnabled").value is False
    assert _char(restored, MULTI_TIER, "StreamingEnabled").value is True


async def test_camera_forwards_to_controllers(
    stream_camera: Callable[..., Camera],
) -> None:
    """Test the camera's session actions reach each controller."""
    camera = stream_camera()
    controllers = [
        *camera.multi_tier_controllers.values(),
        *camera.webrtc_controllers.values(),
    ]
    with (
        patch.object(MultiTierController, "close") as close,
        patch.object(MultiTierController, "client_disconnected") as disconnected,
        patch.object(MultiTierController, "end_session", return_value=True) as end,
        patch.object(MultiTierController, "end_all_sessions") as end_all,
    ):
        await camera.stop()
        camera.client_disconnected(CLIENT)
        assert await camera.end_session(SESSION) is True
        await camera.end_all_sessions()
    close.assert_awaited_once()
    disconnected.assert_called_once_with(CLIENT)
    end.assert_awaited_once_with(SESSION)
    end_all.assert_awaited_once()
    assert len(controllers) == 2


@pytest.mark.parametrize(
    ("multi_tier_ended", "webrtc_ended", "expected"),
    [(True, True, True), (False, True, True), (False, False, False)],
)
async def test_camera_ends_the_id_in_each_controller(
    stream_camera: Callable[..., Camera],
    multi_tier_ended: bool,
    webrtc_ended: bool,
    expected: bool,
) -> None:
    """Test the camera ends a session ID in every controller, not the first only."""
    camera = stream_camera()
    with (
        patch.object(
            MultiTierController, "end_session", return_value=multi_tier_ended
        ) as multi_tier_end,
        patch.object(
            WebRTCController, "end_session", return_value=webrtc_ended
        ) as webrtc_end,
    ):
        assert await camera.end_session(SESSION) is expected
    await camera.stop()

    multi_tier_end.assert_awaited_once_with(SESSION)
    webrtc_end.assert_awaited_once_with(SESSION)
