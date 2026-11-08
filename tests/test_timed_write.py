"""Tests for timed writes (prepare, then write with the pid)."""

from collections.abc import Iterator
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest

from pyhap.accessory import Accessory, get_topic
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (
    HAP_PERMISSION_NOTIFY,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_PID,
    HAP_REPR_STATUS,
    HAP_REPR_TTL,
    HAP_REPR_VALUE,
    HAP_SERVER_STATUS,
)
from pyhap.service import Service

CLIENT = ("1.2.3.4", 5000)
NOW = 1000.0


class WritableChar(NamedTuple):
    """A characteristic of the test accessory and its setter."""

    setter: MagicMock
    aid: int
    iid: int


def _writable(acc: Accessory, service: Service, name: str) -> WritableChar:
    """Give the char ``name`` of ``service`` a mock setter."""
    setter = MagicMock()
    char = service.configure_char(name, setter_callback=setter)
    iid = acc.iid_manager.get_iid(char)
    assert acc.aid is not None
    assert iid is not None
    return WritableChar(setter, acc.aid, iid)


@pytest.fixture
def chars(driver: AccessoryDriver) -> Iterator[dict[str, WritableChar]]:
    """Add a garage door opener with a camera operating mode service.

    Target Door State is a plain write characteristic that iOS still writes
    with timed writes, HomeKit Camera Active has the timed write permission.
    """
    acc = Accessory(driver, "Garage")
    door = driver.loader.get_service("GarageDoorOpener")
    camera = driver.loader.get_service("CameraOperatingMode")
    acc.add_service(door, camera)
    driver.add_accessory(acc)
    with patch.object(driver, "publish"):
        yield {
            "TargetDoorState": _writable(acc, door, "TargetDoorState"),
            "HomeKitCameraActive": _writable(acc, camera, "HomeKitCameraActive"),
        }


@pytest.fixture
def door(chars: dict[str, WritableChar]) -> WritableChar:
    """Return the Target Door State of the test accessory."""
    return chars["TargetDoorState"]


def _write(aid: int, iid: int, pid: int) -> dict[str, Any]:
    """Return a timed write of value 1 that also subscribes to the char."""
    return {
        HAP_REPR_PID: pid,
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: aid,
                HAP_REPR_IID: iid,
                HAP_REPR_VALUE: 1,
                HAP_PERMISSION_NOTIFY: True,
            }
        ],
    }


def test_prepared_write_is_applied(
    driver: AccessoryDriver,
    door: WritableChar,
) -> None:
    """Test a write with a prepared pid inside its TTL is applied."""
    with patch("pyhap.accessory_driver.time.monotonic", return_value=NOW) as now:
        driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 7}, CLIENT)
        now.return_value = NOW + 2.5
        assert driver.set_characteristics(_write(door.aid, door.iid, 7), CLIENT) is None

    door.setter.assert_called_once_with(1)
    assert CLIENT in driver.topics[get_topic(door.aid, door.iid)]


@pytest.mark.parametrize(
    ("write_pid", "delay"),
    [(8, 0.0), (7, 2.6)],
    ids=["unknown_pid", "expired_pid"],
)
def test_unprepared_write_is_ignored(
    driver: AccessoryDriver,
    door: WritableChar,
    write_pid: int,
    delay: float,
) -> None:
    """Test a write with an unknown or expired pid is ignored as a whole."""
    with patch("pyhap.accessory_driver.time.monotonic", return_value=NOW) as now:
        driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 7}, CLIENT)
        now.return_value = NOW + delay
        response = driver.set_characteristics(
            _write(door.aid, door.iid, write_pid), CLIENT
        )

    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: door.aid,
                HAP_REPR_IID: door.iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST,
            }
        ]
    }
    door.setter.assert_not_called()
    assert get_topic(door.aid, door.iid) not in driver.topics


def test_prepared_pid_is_used_once(
    driver: AccessoryDriver,
    door: WritableChar,
) -> None:
    """Test a prepared pid cannot be replayed."""
    driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 7}, CLIENT)
    assert driver.set_characteristics(_write(door.aid, door.iid, 7), CLIENT) is None

    response = driver.set_characteristics(_write(door.aid, door.iid, 7), CLIENT)

    assert response is not None
    assert response[HAP_REPR_CHARS][0][HAP_REPR_STATUS] == (
        HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
    )
    door.setter.assert_called_once_with(1)


def test_prepared_pid_belongs_to_its_connection(
    driver: AccessoryDriver,
    door: WritableChar,
) -> None:
    """Test a pid prepared on one connection is unknown on another."""
    driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 7}, CLIENT)

    response = driver.set_characteristics(
        _write(door.aid, door.iid, 7), ("1.2.3.4", 5001)
    )

    assert response is not None
    assert response[HAP_REPR_CHARS][0][HAP_REPR_STATUS] == (
        HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST
    )
    door.setter.assert_not_called()
