"""Tests for timed writes (prepare, then write with the pid)."""

from collections.abc import Iterator
import logging
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
REJECTED = HAP_SERVER_STATUS.INVALID_VALUE_IN_REQUEST


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
    [(8, 0.0), (1, 2.6), ([1], 0.0), (True, 0.0)],
    ids=["unknown_pid", "expired_pid", "unhashable_pid", "bool_pid"],
)
def test_unprepared_write_is_ignored(
    driver: AccessoryDriver,
    door: WritableChar,
    write_pid: Any,
    delay: float,
) -> None:
    """Test a write with an unknown or expired pid is ignored as a whole."""
    with patch("pyhap.accessory_driver.time.monotonic", return_value=NOW) as now:
        driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 1}, CLIENT)
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


def test_prepared_write_is_kept_until_its_ttl_ends(
    driver: AccessoryDriver,
    door: WritableChar,
) -> None:
    """Test a later prepare at the last moment of a TTL keeps that write."""
    with patch("pyhap.accessory_driver.time.monotonic", return_value=NOW) as now:
        driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 7}, CLIENT)
        now.return_value = NOW + 2.5
        driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 8}, CLIENT)
        assert driver.set_characteristics(_write(door.aid, door.iid, 7), CLIENT) is None

    door.setter.assert_called_once_with(1)


def test_unprepared_write_ignores_every_characteristic(
    driver: AccessoryDriver,
    chars: dict[str, WritableChar],
) -> None:
    """Test a write with an unknown pid writes none of its characteristics."""
    door, camera = chars["TargetDoorState"], chars["HomeKitCameraActive"]
    query = {
        HAP_REPR_PID: 8,
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: door.aid, HAP_REPR_IID: door.iid, HAP_REPR_VALUE: 1},
            {HAP_REPR_AID: camera.aid, HAP_REPR_IID: camera.iid, HAP_REPR_VALUE: 0},
        ],
    }

    response = driver.set_characteristics(query, CLIENT)

    assert _statuses(response) == [REJECTED, REJECTED]
    door.setter.assert_not_called()
    camera.setter.assert_not_called()


@pytest.mark.parametrize(
    "prepare_query",
    [
        {HAP_REPR_TTL: 2500, HAP_REPR_PID: "7"},
        {HAP_REPR_TTL: 2500, HAP_REPR_PID: 1.5},
        {HAP_REPR_TTL: 2500, HAP_REPR_PID: True},
        {HAP_REPR_TTL: "2500", HAP_REPR_PID: 7},
        {HAP_REPR_TTL: 2500.0, HAP_REPR_PID: 7},
        {HAP_REPR_TTL: 10**400, HAP_REPR_PID: 7},
        {HAP_REPR_PID: 7},
        {HAP_REPR_TTL: 2500},
        [2500, 7],
        None,
    ],
    ids=[
        "str_pid",
        "float_pid",
        "bool_pid",
        "str_ttl",
        "float_ttl",
        "overflowing_ttl",
        "missing_ttl",
        "missing_pid",
        "list",
        "null",
    ],
)
def test_prepare_refuses_non_integers(
    driver: AccessoryDriver, prepare_query: Any
) -> None:
    """Test a prepare that is no object or lacks an integer ttl or pid is refused."""
    assert driver.prepare(prepare_query, CLIENT) == {HAP_REPR_STATUS: REJECTED}
    assert driver.prepared_writes.get(CLIENT, {}) == {}


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


def _statuses(response: dict[str, Any] | None) -> list[int]:
    """Return the statuses of a write response, none for a plain success."""
    return [char[HAP_REPR_STATUS] for char in (response or {}).get(HAP_REPR_CHARS, [])]


def _highest_level(records: list[logging.LogRecord]) -> int:
    """Return the highest log level in ``records``, 0 without any."""
    return max((record.levelno for record in records), default=0)


@pytest.mark.parametrize(
    ("enforce", "name", "request_extra", "value", "statuses", "calls", "logged"),
    [
        (True, "HomeKitCameraActive", {}, 0, [REJECTED], 0, 0),
        (True, "HomeKitCameraActive", {HAP_REPR_PID: 7}, 0, [], 1, 0),
        (True, "HomeKitCameraActive", {}, None, [REJECTED], 0, 0),
        (True, "TargetDoorState", {}, 0, [], 1, 0),
        (False, "HomeKitCameraActive", {}, 0, [], 1, 1),
        (False, "HomeKitCameraActive", {}, None, [REJECTED], 0, 0),
        (False, "HomeKitCameraActive", {HAP_REPR_PID: 7}, 0, [], 1, 0),
        (False, "TargetDoorState", {}, 0, [], 1, 0),
    ],
    ids=[
        "enforced_plain_write",
        "enforced_timed_write",
        "enforced_null_value",
        "enforced_plain_write_without_tw",
        "lenient_plain_write",
        "lenient_null_value",
        "lenient_timed_write",
        "lenient_plain_write_without_tw",
    ],
)
def test_timed_write_permission(
    driver: AccessoryDriver,
    chars: dict[str, WritableChar],
    caplog: pytest.LogCaptureFixture,
    enforce: bool,
    name: str,
    request_extra: dict[str, Any],
    value: int | None,
    statuses: list[int],
    calls: int,
    logged: int,
) -> None:
    """Test a plain write to a timed write characteristic is refused or logged."""
    caplog.set_level(logging.DEBUG, logger="pyhap.accessory_driver")
    char = chars[name]
    driver.enforce_timed_writes = enforce
    driver.prepare({HAP_REPR_TTL: 2500, HAP_REPR_PID: 7}, CLIENT)
    query = {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_VALUE: value}
        ],
        **request_extra,
    }

    response = driver.set_characteristics(query, CLIENT)

    assert _statuses(response) == statuses
    assert char.setter.call_count == calls
    assert caplog.text.count("requires a timed write") == logged
    # Lenient mode must not warn, hubs write these values all the time.
    assert _highest_level(caplog.records) < logging.WARNING


def test_enforce_timed_writes_defaults_to_off(driver: AccessoryDriver) -> None:
    """Test timed writes are only enforced on request."""
    assert driver.enforce_timed_writes is False
    enforced = AccessoryDriver(loop=driver.loop, enforce_timed_writes=True)
    assert enforced.enforce_timed_writes is True
