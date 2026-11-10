"""Tests for the HKSV 17.99 camera capabilities of pyhap.camera."""

import asyncio
from collections.abc import AsyncIterator
import logging
import pathlib
from typing import Any
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from pyhap import hksv3
from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import HKSV3_STATE, Camera
from pyhap.const import (
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_STATUS,
    HAP_REPR_VALUE,
    HAP_SERVER_STATUS,
)
from pyhap.util import base64_to_bytes, to_base64_str

CAMERA_AID = 2
MAIN = UUID("00112233-4455-6677-8899-aabbccddeeff")
PACKAGE = UUID("ffeeddcc-bbaa-9988-7766-554433221100")
SENSORS = [
    hksv3.SensorConfiguration(
        MAIN,
        2688,
        1520,
        video_streams=[
            hksv3.VideoStreamCapability(
                PACKAGE, hksv3.VideoQuality.HIGH, 1920, 1080, 30, 1700, 1800
            )
        ],
    ),
    hksv3.SensorConfiguration(
        PACKAGE, 1600, 1200, hksv3.SensorType.GENERIC, hksv3.SensorIntent.PACKAGE
    ),
]
ZONES = hksv3.CameraZones(
    [
        hksv3.ZoneData(
            hksv3.ZoneMethod.NORMAL,
            [hksv3.ZonePolygon(MAIN, [(0, 0), (1344, 0), (1344, 760)])],
        )
    ]
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


def _chars(camera: Camera, service: str) -> list[str | None] | None:
    found = camera.get_service(service)
    if found is None:
        return None
    return [char.display_name for char in found.characteristics]


@pytest.mark.parametrize(
    ("recording", "motion_chars"),
    [(False, None), (True, ["MotionDetected", "StatusActive"])],
)
async def test_without_sensors(
    driver: Any, recording: bool, motion_chars: list[str] | None
) -> None:
    """Test cameras without the sensors option are unchanged."""
    camera = _camera(driver, recording=recording)
    assert camera.get_service("CameraCapabilities") is None
    assert camera.get_service("CameraMotionZones") is None
    assert _chars(camera, "MotionSensor") == motion_chars
    assert camera.camera_zones is None
    assert camera.motion_zones_active is False
    assert camera.motion_enabled is True
    camera.set_motion_detected(True, [MAIN])


async def test_sensor_minimum_streams(
    driver: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Test sensors with fewer than three encodings log a warning."""
    stream = SENSORS[0].video_streams[0]
    sensors = [
        hksv3.SensorConfiguration(MAIN, 1920, 1080, video_streams=[stream] * 3),
        *SENSORS[1:],
    ]
    with caplog.at_level(logging.WARNING):
        _camera(driver, sensors=sensors)
    assert caplog.messages == [
        f"Cam: sensor {PACKAGE} has 0 video encodings, HKSV needs at least 3"
    ]


async def test_services(driver: Any) -> None:
    """Test the capabilities and motion zones services."""
    camera = _camera(driver, sensors=SENSORS)
    assert _char(camera, "CameraCapabilities", "Version").value == "17.99"
    capabilities = _char(camera, "CameraCapabilities", "CameraCapabilities")
    assert base64_to_bytes(capabilities.value) == (
        hksv3.CameraCapabilities(SENSORS).encode()
    )
    assert _char(camera, "CameraMotionZones", "Version").value == "17.99"
    assert _chars(camera, "MotionSensor") == [
        "MotionDetected",
        "ContributingSensors",
        "MotionEnabled",
    ]
    assert _char(camera, "MotionSensor", "MotionEnabled").value is True


async def test_recording_motion_sensor(driver: Any) -> None:
    """Test recording and capabilities share one motion sensor."""
    camera = _camera(driver, recording=True, sensors=SENSORS)
    motion = [s for s in camera.services if s.display_name == "MotionSensor"]
    assert len(motion) == 1
    assert _chars(camera, "MotionSensor") == [
        "MotionDetected",
        "StatusActive",
        "ContributingSensors",
        "MotionEnabled",
    ]


def _contributing(camera: Camera) -> bytes:
    return base64_to_bytes(_char(camera, "MotionSensor", "ContributingSensors").value)


@pytest.mark.parametrize("default", [None, []])
async def test_contributing_sensors(driver: Any, default: list[UUID] | None) -> None:
    """Test motion names the sensors that saw it."""
    camera = _camera(driver, sensors=SENSORS)
    detected = _char(camera, "MotionSensor", "MotionDetected")

    camera.set_motion_detected(True, default)
    assert detected.value is True
    assert _contributing(camera) == hksv3.encode_contributing_sensors([MAIN])

    camera.set_motion_detected(True, [MAIN, PACKAGE])
    assert _contributing(camera) == hksv3.encode_contributing_sensors([MAIN, PACKAGE])

    camera.set_motion_detected(False, [PACKAGE])
    assert detected.value is False
    assert _contributing(camera) == b""


async def test_contributing_sensors_unknown(
    driver: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Test motion from a sensor that is not configured logs a warning."""
    camera = _camera(driver, sensors=SENSORS)
    other = UUID(int=1)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        camera.set_motion_detected(True, iter([MAIN, other]))
    assert caplog.messages == [
        f"Cam: motion reported for sensors that are not configured: {other}"
    ]
    assert _contributing(camera) == hksv3.encode_contributing_sensors([MAIN, other])


async def test_motion_enabled(driver: Any) -> None:
    """Test turning motion detection off clears and suppresses motion."""
    camera = _camera(driver, sensors=SENSORS)
    camera.motion_enabled_changed = MagicMock()
    detected = _char(camera, "MotionSensor", "MotionDetected")
    enabled = _char(camera, "MotionSensor", "MotionEnabled")
    camera.set_motion_detected(True)

    enabled.client_update_value(False)
    camera.motion_enabled_changed.assert_called_once_with(False)
    assert camera.motion_enabled is False
    assert detected.value is False
    camera.set_motion_detected(True)
    assert detected.value is False
    assert _contributing(camera) == b""

    enabled.client_update_value(True)
    camera.motion_enabled_changed.assert_called_with(True)
    camera.set_motion_detected(True)
    assert detected.value is True


async def test_motion_zones(driver: Any) -> None:
    """Test the controller writing and clearing motion zones."""
    camera = _camera(driver, sensors=SENSORS)
    camera.motion_zones_changed = MagicMock()
    zones = _char(camera, "CameraMotionZones", "CameraZones")

    _char(camera, "CameraMotionZones", "Active").client_update_value(1)
    assert camera.motion_zones_active is True
    zones.client_update_value(to_base64_str(ZONES.encode()))
    assert camera.camera_zones == ZONES
    assert camera.motion_zones_changed.call_count == 2

    zones.client_update_value("")
    assert camera.camera_zones is None
    assert camera.motion_zones_changed.call_count == 3


@pytest.mark.parametrize("value", ["AQ==", "!!"])
@pytest.mark.parametrize(
    ("previous", "kept"), [(None, ""), (ZONES, to_base64_str(ZONES.encode()))]
)
async def test_motion_zones_malformed(
    driver: Any, value: str, previous: hksv3.CameraZones | None, kept: str
) -> None:
    """Test a malformed zones write fails and keeps the previous zones."""
    camera = _camera(driver, sensors=SENSORS)
    _char(camera, "CameraMotionZones", "Active").client_update_value(1)
    zones = _char(camera, "CameraMotionZones", "CameraZones")
    zones.client_update_value(kept)
    camera.motion_zones_changed = MagicMock()
    with pytest.raises(ValueError):
        zones.client_update_value(value)
    camera.motion_zones_changed.assert_not_called()
    assert zones.value == kept
    assert camera.camera_zones == previous
    assert camera.motion_zones_active is True


async def test_motion_zones_damaged_state(driver: Any) -> None:
    """Test stored zones that do not decode count as no zones."""
    driver.accessory_state(CAMERA_AID)[HKSV3_STATE] = {"camera_zones": "AQ=="}
    camera = _camera(driver, sensors=SENSORS)
    assert _char(camera, "CameraMotionZones", "CameraZones").value == "AQ=="
    assert camera.camera_zones is None


async def test_motion_zones_malformed_write(driver: Any) -> None:
    """Test a controller reading the zones after a failed write gets the old ones."""
    camera = Camera({"address": "127.0.0.1", "sensors": SENSORS}, driver, "Cam")
    driver.add_accessory(camera)
    zones = _char(camera, "CameraMotionZones", "CameraZones")
    iid = camera.iid_manager.get_iid(zones)
    kept = to_base64_str(ZONES.encode())
    zones.client_update_value(kept)

    response = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: 1, HAP_REPR_IID: iid, HAP_REPR_VALUE: "AQ=="}
            ]
        },
        ("127.0.0.1", 5000),
    )
    assert response is not None
    assert response[HAP_REPR_CHARS][0][HAP_REPR_STATUS] == (
        HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE
    )
    read = driver.get_characteristics([f"1.{iid}"])
    assert read[HAP_REPR_CHARS][0][HAP_REPR_VALUE] == kept


async def test_settings_survive_restart(driver: Any) -> None:
    """Test zones and motion settings are restored."""
    camera = _camera(driver, sensors=SENSORS)
    _char(camera, "CameraMotionZones", "Active").client_update_value(1)
    _char(camera, "CameraMotionZones", "CameraZones").client_update_value(
        to_base64_str(ZONES.encode())
    )
    _char(camera, "MotionSensor", "MotionEnabled").client_update_value(False)

    camera = _camera(driver, sensors=SENSORS)
    assert camera.motion_zones_active is True
    assert camera.camera_zones == ZONES
    assert camera.motion_enabled is False


async def test_reset_calls_hooks(driver: Any) -> None:
    """Test removing the last pairing tells the integration what changed."""
    camera = _camera(driver, sensors=SENSORS)
    _char(camera, "CameraMotionZones", "Active").client_update_value(1)
    _char(camera, "CameraMotionZones", "CameraZones").client_update_value(
        to_base64_str(ZONES.encode())
    )
    _char(camera, "MotionSensor", "MotionEnabled").client_update_value(False)
    camera.motion_zones_changed = MagicMock()
    camera.motion_enabled_changed = MagicMock()

    camera.reset_accessory_state()
    camera.motion_zones_changed.assert_called_once_with()
    camera.motion_enabled_changed.assert_called_once_with(True)
    assert camera.camera_zones is None
    assert camera.motion_zones_active is False
    assert camera.motion_enabled is True


async def test_reset_unchanged_calls_no_hooks(driver: Any) -> None:
    """Test a reset without stored settings calls no hooks."""
    camera = _camera(driver, sensors=SENSORS)
    camera.motion_zones_changed = MagicMock()
    camera.motion_enabled_changed = MagicMock()

    camera.reset_accessory_state()
    camera.motion_zones_changed.assert_not_called()
    camera.motion_enabled_changed.assert_not_called()


@pytest.mark.parametrize(
    ("service", "name", "value", "hook", "args"),
    [
        ("CameraMotionZones", "Active", 1, "motion_zones_changed", ()),
        (
            "CameraMotionZones",
            "CameraZones",
            to_base64_str(ZONES.encode()),
            "motion_zones_changed",
            (),
        ),
        ("MotionSensor", "MotionEnabled", False, "motion_enabled_changed", (False,)),
    ],
)
async def test_repeated_write_calls_hook_once(
    driver: Any, service: str, name: str, value: Any, hook: str, args: tuple[Any, ...]
) -> None:
    """Test a hub writing the same value again does not call the hook again."""
    camera = _camera(driver, sensors=SENSORS)
    mock = MagicMock()
    setattr(camera, hook, mock)
    char = _char(camera, service, name)

    char.client_update_value(value)
    char.client_update_value(value)
    char.client_update_value(value)
    mock.assert_called_once_with(*args)


@pytest.mark.parametrize(
    ("service", "name", "value", "hook"),
    [
        ("CameraMotionZones", "Active", 0, "motion_zones_changed"),
        ("MotionSensor", "MotionEnabled", True, "motion_enabled_changed"),
    ],
)
async def test_write_of_current_value_calls_no_hook(
    driver: Any, service: str, name: str, value: Any, hook: str
) -> None:
    """Test writing the value the camera already has is no change."""
    camera = _camera(driver, sensors=SENSORS)
    mock = MagicMock()
    setattr(camera, hook, mock)

    _char(camera, service, name).client_update_value(value)
    mock.assert_not_called()


async def test_reset_hooks_see_reset_recording(driver: Any) -> None:
    """Test the hooks run after the recording state is reset as well."""
    camera = _camera(driver, sensors=SENSORS, recording=True)
    _char(camera, "CameraRecordingManagement", "Active").client_update_value(1)
    _char(camera, "CameraMotionZones", "Active").client_update_value(1)
    seen: list[bool] = []
    camera.motion_zones_changed = lambda: seen.append(camera.recording_active)

    camera.reset_accessory_state()
    assert seen == [False]
