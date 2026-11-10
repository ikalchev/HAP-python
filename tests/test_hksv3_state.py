"""Tests for persisting the HKSV 17.99 camera state."""

import asyncio
from collections.abc import AsyncIterator
import logging
import pathlib
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

from pyhap.accessory_driver import AccessoryDriver
from pyhap.camera import HKSV3_STATE, Camera

CAMERA_AID = 2


@pytest.fixture(name="driver")
async def driver_fixture(tmp_path: pathlib.Path) -> AsyncIterator[AccessoryDriver]:
    with patch("pyhap.accessory_driver.AccessoryDriver.async_persist"):
        yield AccessoryDriver(
            loop=asyncio.get_running_loop(),
            listen_address="127.0.0.1",
            persist_file=str(tmp_path / "accessory.state"),
        )


async def _drain() -> None:
    # add_job hops through call_soon_threadsafe, then call_soon.
    for _ in range(2):
        await asyncio.sleep(0)


def _camera(
    driver: Any, aid: int | None = CAMERA_AID, handler: Any = None
) -> tuple[Camera, Any, Any]:
    camera = Camera({"address": "127.0.0.1"}, driver, "Cam", aid=aid)
    light = camera.add_preload_service("Lightbulb", chars=["Brightness"])
    on, brightness = (
        light.get_characteristic("On"),
        light.get_characteristic("Brightness"),
    )
    handler = handler or MagicMock()
    camera._persist_hksv3_char(on, "on", handler)
    camera._persist_hksv3_char(brightness, "brightness", handler)
    camera._restore_hksv3_state()
    return camera, on, brightness


async def test_write_is_stored_and_restored(driver: Any) -> None:
    """Test a controller write survives rebuilding the camera."""
    _, on, brightness = _camera(driver)
    on.client_update_value(True)
    brightness.client_update_value(40)
    await _drain()
    assert driver.accessory_state(CAMERA_AID)[HKSV3_STATE] == {
        "on": True,
        "brightness": 40,
    }
    # Both writes share one save.
    driver.async_persist.assert_called_once()

    _, on, brightness = _camera(driver)
    assert on.value is True
    assert brightness.value == 40


async def test_restore_skips_invalid_values(
    driver: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Test a damaged state entry keeps the default."""
    driver.accessory_state(CAMERA_AID)[HKSV3_STATE] = {"brightness": "bright"}
    with caplog.at_level(logging.WARNING):
        _, on, brightness = _camera(driver)
    assert on.value is False
    assert brightness.value == 0
    assert "Ignoring stored brightness" in caplog.text


async def test_restore_ignores_other_state(driver: Any) -> None:
    """Test a state entry that is not a dict is ignored."""
    driver.accessory_state(CAMERA_AID)[HKSV3_STATE] = ["on"]
    _, on, _ = _camera(driver)
    assert on.value is False


async def test_write_replaces_other_state(driver: Any) -> None:
    """Test a write still works when the state entry is not a dict."""
    driver.accessory_state(CAMERA_AID)[HKSV3_STATE] = ["on"]
    _, on, _ = _camera(driver)
    on.client_update_value(True)
    assert driver.accessory_state(CAMERA_AID)[HKSV3_STATE] == {"on": True}


async def test_restore_accessory_state(driver: Any) -> None:
    """Test the driver restoring state loaded after construction."""
    camera, on, _ = _camera(driver)
    driver.accessory_state(CAMERA_AID)[HKSV3_STATE] = {"on": True}
    camera.restore_accessory_state()
    assert on.value is True


async def test_reset_returns_defaults(driver: Any) -> None:
    """Test removing the last pairing forgets the stored state."""
    camera, on, brightness = _camera(driver)
    on.client_update_value(True)
    brightness.client_update_value(40)
    await _drain()
    driver.async_persist.reset_mock()

    camera.reset_accessory_state()
    await _drain()
    assert on.value is False
    assert brightness.value == 0
    assert HKSV3_STATE not in driver.accessory_state(CAMERA_AID)
    driver.async_persist.assert_called_once()


@pytest.mark.parametrize(
    ("on_value", "brightness_value", "calls"),
    [
        (True, 40, [call(False)]),
        (False, 40, [call(0)]),
        (False, 0, []),
    ],
)
async def test_reset_calls_handler(
    driver: Any, on_value: bool, brightness_value: int, calls: list[Any]
) -> None:
    """Test a reset calls each handler once, only for changed values."""
    handler = MagicMock()
    camera, on, brightness = _camera(driver, handler=handler)
    on.set_value(on_value)
    brightness.set_value(brightness_value)

    camera.reset_accessory_state()
    assert handler.call_args_list == calls


async def test_without_aid_nothing_is_stored(driver: Any) -> None:
    """Test a camera without aid neither stores nor restores."""
    camera, on, _ = _camera(driver, aid=None)
    on.client_update_value(True)
    camera.reset_accessory_state()
    await _drain()
    assert driver.state.accessory_states == {}
    driver.async_persist.assert_not_called()


async def test_save_without_loop(driver: Any, caplog: pytest.LogCaptureFixture) -> None:
    """Test a closed loop only logs."""
    _, on, _ = _camera(driver)
    with (
        patch.object(driver, "add_job", side_effect=RuntimeError),
        caplog.at_level(logging.WARNING),
    ):
        on.client_update_value(True)
    assert "Could not schedule saving the camera state" in caplog.text
