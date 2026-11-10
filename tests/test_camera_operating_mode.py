"""Tests for the HKSV 17.99 global operating mode of pyhap.camera."""

import asyncio
from collections.abc import AsyncIterator
import pathlib
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

import pytest

from pyhap import camera as camera_module, hds_recording, tlv
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import SETUP_STATUS, SETUP_TYPES, STREAMING_STATUS, Camera
from pyhap.characteristic import PROP_PERMISSIONS

CAMERA_AID = 2
GLOBAL = "CameraGlobalOperatingMode"
OPTIONAL = ["ManuallyDisabled", "NightVision", "ThirdPartyCameraActive"]
VIDEO = {
    "codec": {
        "profiles": [camera_module.VIDEO_CODEC_PARAM_PROFILE_ID_TYPES["MAIN"]],
        "levels": [camera_module.VIDEO_CODEC_PARAM_LEVEL_TYPES["TYPE4_0"]],
    },
    "resolutions": [[640, 360, 30]],
}
AUDIO = {"codecs": [{"type": "OPUS", "samplerate": 24}]}
# A stream start request for SESSION_ID.
START_REQUEST = (
    "ARUCAQEBEKzMbMEFY0UVjal0tFCQBpECNAEBAAIJAQEAAgEAAwEAAwsBAoAC"
    "AgJoAQMBHgQXAQFjAgQr66FSAwKEAAQEAAAAPwUCYgUDLAEBAgIMAQEBAgEA"
    "AwEBBAEeAxYBAW4CBMUInmQDAhgABAQAAKBABgENBAEA"
)
SESSION_ID = UUID("accc6cc1-0563-4515-8da9-74b450900691")
# A SetupEndpoints request for SESSION_ID.
SETUP_REQUEST = (
    "ARCszGzBBWNFFY2pdLRQkAaRAxoBAQACDTE5Mi4xNjguMS4xMTQDAjPFBAKs1gQ"
    "lAhDYlmCkyTBZQfxqFS3OnxVOAw4bQZm5NuoQjyanlqWA0QEBAAUlAhAKRPSRVa"
    "qGeNmESTIojxNiAw78WkjTLtGv0waWnLo9gQEBAA=="
)


@pytest.fixture(name="driver")
async def driver_fixture(tmp_path: pathlib.Path) -> AsyncIterator[AccessoryDriver]:
    with patch("pyhap.accessory_driver.AccessoryDriver.async_persist"):
        yield AccessoryDriver(
            loop=asyncio.get_running_loop(),
            listen_address="127.0.0.1",
            persist_file=str(tmp_path / "accessory.state"),
        )


def _camera(driver: Any, **options: Any) -> Camera:
    return Camera({"address": "127.0.0.1", **options}, driver, "Cam", aid=CAMERA_AID)


def _char(camera: Camera, service: str, name: str) -> Any:
    found = camera.get_service(service)
    assert found is not None
    return found.get_characteristic(name)


def _values(camera: Camera) -> dict[str | None, Any]:
    service = camera.get_service(GLOBAL)
    assert service is not None
    return {char.display_name: char.value for char in service.characteristics}


async def test_without_option(driver: Any) -> None:
    """Test cameras without the option are unchanged."""
    camera = _camera(driver)
    assert camera.get_service(GLOBAL) is None
    assert camera.homekit_camera_active is True
    assert camera.streaming_allowed is True
    camera.set_manually_disabled(True)
    camera.set_third_party_camera_active(False)


async def test_required_chars(driver: Any) -> None:
    """Test an empty list adds the required chars, all on."""
    camera = _camera(driver, global_operating_mode=[])
    assert _values(camera) == {
        "HomeKitCameraActive": 1,
        "StreamingEnabled": True,
        "CameraOperatingModeIndicator": True,
    }
    assert camera.streaming_allowed is True
    camera.set_manually_disabled(True)
    camera.set_third_party_camera_active(False)
    assert camera.streaming_allowed is True


async def test_optional_chars(driver: Any) -> None:
    """Test optional chars and their defaults."""
    camera = _camera(driver, global_operating_mode=OPTIONAL)
    assert _values(camera) == {
        "HomeKitCameraActive": 1,
        "StreamingEnabled": True,
        "CameraOperatingModeIndicator": True,
        "ManuallyDisabled": False,
        "NightVision": False,
        "ThirdPartyCameraActive": True,
    }


async def test_manually_disabled(driver: Any) -> None:
    """Test the device turning the camera off stops streaming."""
    camera = _camera(driver, global_operating_mode=OPTIONAL)
    camera.set_manually_disabled(True)
    assert _char(camera, GLOBAL, "ManuallyDisabled").value is True
    assert camera.streaming_allowed is False
    assert camera.homekit_camera_active is True
    camera.set_manually_disabled(False)
    assert camera.streaming_allowed is True


async def test_third_party_camera_active(driver: Any) -> None:
    """Test the device reports Third Party Camera Active, controllers only read it."""
    camera = _camera(driver, global_operating_mode=OPTIONAL)
    camera.global_operating_mode_changed = MagicMock()
    char = _char(camera, GLOBAL, "ThirdPartyCameraActive")
    assert char.properties[PROP_PERMISSIONS] == ["pr", "ev"]
    assert char.setter_callback is None
    camera.set_third_party_camera_active(False)
    assert char.value is False
    assert camera.streaming_allowed is True
    camera.global_operating_mode_changed.assert_not_called()


@pytest.mark.parametrize(
    ("name", "value", "streaming_allowed"),
    [
        ("StreamingEnabled", False, False),
        ("CameraOperatingModeIndicator", False, True),
        ("NightVision", True, True),
    ],
)
async def test_writes_call_hook(
    driver: Any, name: str, value: bool, streaming_allowed: bool
) -> None:
    """Test controller writes reach the hook with the setting and its value."""
    camera = _camera(driver, global_operating_mode=OPTIONAL)
    camera.global_operating_mode_changed = MagicMock()
    camera.homekit_camera_active_changed = MagicMock()
    _char(camera, GLOBAL, name).client_update_value(value)
    camera.global_operating_mode_changed.assert_called_once_with(name, value)
    camera.homekit_camera_active_changed.assert_not_called()
    assert camera.streaming_allowed is streaming_allowed


@pytest.mark.parametrize(
    ("enabled", "starts", "status", "warnings"),
    [
        (True, 1, STREAMING_STATUS["STREAMING"], 0),
        (False, 0, STREAMING_STATUS["AVAILABLE"], 1),
    ],
)
async def test_stream_start_needs_streaming_allowed(
    driver: Any,
    enabled: bool,
    starts: int,
    status: bytes,
    warnings: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a stream turned off after its setup does not start."""
    camera = _camera(driver, video=VIDEO, audio=AUDIO, global_operating_mode=[])
    camera.start_stream = AsyncMock(return_value=True)
    camera.sessions[SESSION_ID] = {"id": SESSION_ID, "stream_idx": 0}
    _char(camera, GLOBAL, "StreamingEnabled").client_update_value(enabled)

    await camera._start_stream(
        tlv.decode(START_REQUEST, from_base64=True), reconfigure=False
    )
    assert camera.start_stream.await_count == starts
    assert camera.streaming_status == status
    assert (SESSION_ID in camera.sessions) is enabled
    assert caplog.text.count("Streaming is turned off") == warnings
    assert "Failed to start/reconfigure stream" not in caplog.text


@pytest.mark.parametrize(
    ("options", "service", "enabled", "status", "sessions"),
    [
        (
            {"global_operating_mode": []},
            GLOBAL,
            True,
            SETUP_STATUS["SUCCESS"],
            [SESSION_ID],
        ),
        ({"global_operating_mode": []}, GLOBAL, False, SETUP_STATUS["ERROR"], []),
        (
            {"recording": True},
            "CameraOperatingMode",
            False,
            SETUP_STATUS["SUCCESS"],
            [SESSION_ID],
        ),
    ],
)
async def test_setup_endpoints_needs_streaming_allowed(
    driver: Any,
    options: dict[str, Any],
    service: str,
    enabled: bool,
    status: bytes,
    sessions: list[UUID],
) -> None:
    """Test the camera refuses a stream setup while streaming is turned off.

    Without the global operating mode, turning the camera off does not
    refuse streams, as before.
    """
    camera = _camera(driver, video=VIDEO, audio=AUDIO, **options)
    _char(camera, service, "HomeKitCameraActive").client_update_value(enabled)
    setup = _char(camera, "CameraRTPStreamManagement", "SetupEndpoints")
    answer = setup.client_update_value(SETUP_REQUEST)

    response = tlv.decode(answer, from_base64=True)
    assert response[SETUP_TYPES["SESSION_ID"]] == SESSION_ID.bytes
    assert response[SETUP_TYPES["STATUS"]] == status
    assert list(camera.sessions) == sessions


async def test_homekit_camera_active(driver: Any) -> None:
    """Test turning the camera off without recording."""
    camera = _camera(driver, global_operating_mode=[])
    camera.homekit_camera_active_changed = MagicMock()
    _char(camera, GLOBAL, "HomeKitCameraActive").client_update_value(0)
    camera.homekit_camera_active_changed.assert_called_once_with(False)
    assert camera.homekit_camera_active is False
    assert camera.streaming_allowed is False


async def test_global_operating_mode_decides(driver: Any) -> None:
    """Test the global operating mode turns a recording camera off."""
    camera = _camera(driver, recording=True, global_operating_mode=[])
    camera.homekit_camera_active_changed = MagicMock()
    stream = MagicMock()
    camera._recording_stream = stream
    status_active = _char(camera, "MotionSensor", "StatusActive")

    _char(camera, GLOBAL, "HomeKitCameraActive").client_update_value(0)
    camera.homekit_camera_active_changed.assert_called_once_with(False)
    assert camera.homekit_camera_active is False
    assert status_active.get_value() is False
    stream.close.assert_called_once_with(hds_recording.RecordingReason.NOT_ALLOWED)


async def test_operating_mode_ignored(driver: Any) -> None:
    """Test the Camera Operating Mode value is ignored next to the global one."""
    camera = _camera(driver, recording=True, global_operating_mode=[])
    camera.homekit_camera_active_changed = MagicMock()
    stream = MagicMock()
    camera._recording_stream = stream
    status_active = _char(camera, "MotionSensor", "StatusActive")
    active = _char(camera, GLOBAL, "HomeKitCameraActive")
    legacy = _char(camera, "CameraOperatingMode", "HomeKitCameraActive")

    # A value left from before must not keep the camera off.
    active.client_update_value(0)
    legacy.client_update_value(0)
    active.client_update_value(1)
    assert camera.homekit_camera_active_changed.call_count == 2
    camera.homekit_camera_active_changed.assert_called_with(True)
    assert camera.homekit_camera_active is True
    assert camera.streaming_allowed is True
    assert status_active.get_value() is True

    legacy.client_update_value(1)
    legacy.client_update_value(0)
    assert camera.homekit_camera_active_changed.call_count == 2
    assert camera.homekit_camera_active is True
    stream.close.assert_called_once()


async def test_settings_survive_restart(driver: Any) -> None:
    """Test controller writes are restored, the device state is not."""
    camera = _camera(driver, global_operating_mode=OPTIONAL)
    _char(camera, GLOBAL, "HomeKitCameraActive").client_update_value(0)
    _char(camera, GLOBAL, "StreamingEnabled").client_update_value(False)
    _char(camera, GLOBAL, "CameraOperatingModeIndicator").client_update_value(False)
    _char(camera, GLOBAL, "NightVision").client_update_value(True)
    camera.set_manually_disabled(True)
    camera.set_third_party_camera_active(False)

    camera = _camera(driver, global_operating_mode=OPTIONAL)
    assert _values(camera) == {
        "HomeKitCameraActive": 0,
        "StreamingEnabled": False,
        "CameraOperatingModeIndicator": False,
        "ManuallyDisabled": False,
        "NightVision": True,
        "ThirdPartyCameraActive": True,
    }
    camera.global_operating_mode_changed = MagicMock()
    camera.homekit_camera_active_changed = MagicMock()

    camera.reset_accessory_state()
    camera.homekit_camera_active_changed.assert_called_once_with(True)
    assert camera.global_operating_mode_changed.call_args_list == [
        call("StreamingEnabled", True),
        call("CameraOperatingModeIndicator", True),
        call("NightVision", False),
    ]
    assert _values(camera) == {
        "HomeKitCameraActive": 1,
        "StreamingEnabled": True,
        "CameraOperatingModeIndicator": True,
        "ManuallyDisabled": False,
        "NightVision": False,
        "ThirdPartyCameraActive": True,
    }


def _instance_ids(
    camera: Camera,
) -> dict[tuple[str | None, str | None], int | None]:
    return {
        (service.display_name, char.display_name): camera.iid_manager.get_iid(char)
        for service in camera.services
        for char in service.characteristics
    }


async def test_instance_ids_unchanged(driver: Any) -> None:
    """Test turning the option on keeps the instance IDs of the other services."""
    without = _instance_ids(_camera(driver, recording=True, video=VIDEO, audio=AUDIO))
    with_option = _instance_ids(
        _camera(
            driver, recording=True, video=VIDEO, audio=AUDIO, global_operating_mode=[]
        )
    )
    assert without.items() <= with_option.items()


@pytest.mark.parametrize(
    ("name", "value", "hook", "args"),
    [
        (
            "StreamingEnabled",
            False,
            "global_operating_mode_changed",
            ("StreamingEnabled", False),
        ),
        ("NightVision", True, "global_operating_mode_changed", ("NightVision", True)),
        ("HomeKitCameraActive", 0, "homekit_camera_active_changed", (False,)),
    ],
)
async def test_repeated_write_calls_hook_once(
    driver: Any, name: str, value: Any, hook: str, args: tuple[Any, ...]
) -> None:
    """Test a hub writing the same value again does not call the hook again."""
    camera = _camera(driver, recording=True, global_operating_mode=OPTIONAL)
    mock = MagicMock()
    setattr(camera, hook, mock)
    char = _char(camera, GLOBAL, name)

    char.client_update_value(value)
    char.client_update_value(value)
    char.client_update_value(value)
    mock.assert_called_once_with(*args)


async def test_classic_writes_call_hook_every_time(driver: Any) -> None:
    """Test cameras without the option keep calling the hook on every write."""
    camera = _camera(driver, recording=True)
    camera.homekit_camera_active_changed = MagicMock()
    char = _char(camera, "CameraOperatingMode", "HomeKitCameraActive")

    char.client_update_value(0)
    char.client_update_value(0)
    assert camera.homekit_camera_active_changed.call_args_list == [call(False)] * 2


@pytest.mark.parametrize("chars", [["Brightness"], ["HomeKitCameraActive"]])
async def test_unknown_chars(driver: Any, chars: list[str]) -> None:
    """Test names that are not optional chars of the service raise."""
    with pytest.raises(ValueError, match=chars[0]):
        _camera(driver, global_operating_mode=chars)


@pytest.mark.parametrize("chars", [True, "NightVision"])
async def test_option_not_a_list(driver: Any, chars: Any) -> None:
    """Test the option must be a list of names."""
    with pytest.raises(ValueError, match="must be a list"):
        _camera(driver, global_operating_mode=chars)
