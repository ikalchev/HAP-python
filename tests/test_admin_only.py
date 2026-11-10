"""Tests for characteristics restricted to admin controllers."""

from collections.abc import Callable, Iterator
import io
import json
from typing import Any, NamedTuple
from unittest.mock import MagicMock, PropertyMock, patch
from urllib.parse import urlparse
from uuid import UUID

import pytest

from pyhap import hap_handler
from pyhap.accessory import Accessory
from pyhap.accessory_driver import AccessoryDriver
from pyhap.characteristic import (
    HAP_FORMAT_BOOL,
    HAP_PERMISSION_READ,
    PROP_ADMIN_ONLY_ACCESS,
    PROP_FORMAT,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.const import (
    HAP_PERMISSION_WRITE,
    HAP_PERMISSIONS,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_STATUS,
    HAP_REPR_VALUE,
    HAP_SERVER_STATUS,
)
from pyhap.encoder import AccessoryEncoder
from pyhap.service import Service

CLIENT = ("1.2.3.4", 5000)
ADMIN = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c1")
USER = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c2")
UNPAIRED = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c3")
OTHER_ADMIN = UUID("7d0d1ee9-46fe-4a56-a115-69df3f6860c4")
REFUSED = HAP_SERVER_STATUS.INSUFFICIENT_PRIVILEGES


class SensorChar(NamedTuple):
    """A characteristic of the test accessory and its setter."""

    setter: MagicMock
    aid: int
    iid: int


def _with_setter(acc: Accessory, service: Service, name: str) -> SensorChar:
    """Give the char ``name`` of ``service`` a mock setter."""
    setter = MagicMock()
    char = service.configure_char(name, setter_callback=setter)
    iid = acc.iid_manager.get_iid(char)
    assert acc.aid is not None
    assert iid is not None
    return SensorChar(setter, acc.aid, iid)


def _write(char: SensorChar) -> dict[str, Any]:
    """Return a write of True to ``char``."""
    return {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_VALUE: True}
        ]
    }


def _response(char: SensorChar, status: int | None) -> dict[str, Any] | None:
    """Return the expected write response, None for a plain success."""
    if status is None:
        return None
    return {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_STATUS: status}
        ]
    }


@pytest.fixture
def chars(driver: AccessoryDriver) -> Iterator[dict[str, SensorChar]]:
    """Add a motion sensor and pair an admin and a regular controller.

    Motion Enabled may only be written by admins. Secret is a made-up
    characteristic that only admins may read. Open is a made-up characteristic
    anyone may read and write. Sealed is made up, write only and readable only
    by admins, Locked is made up, read only and writable only by admins.
    """
    acc = Accessory(driver, "Sensor")
    service = driver.loader.get_service("MotionSensor")
    service.add_characteristic(
        driver.loader.get_char("MotionEnabled"),
        Characteristic(
            "Open",
            UUID("2d7f7c38-1d4c-4e0b-9d0e-4c1f1d2c7a11"),
            {
                PROP_FORMAT: HAP_FORMAT_BOOL,
                PROP_PERMISSIONS: [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE],
            },
        ),
        Characteristic(
            "Secret",
            UUID("2d7f7c38-1d4c-4e0b-9d0e-4c1f1d2c7a10"),
            {
                PROP_FORMAT: HAP_FORMAT_BOOL,
                PROP_PERMISSIONS: [HAP_PERMISSION_READ],
                PROP_ADMIN_ONLY_ACCESS: ["read"],
            },
        ),
        Characteristic(
            "Sealed",
            UUID("2d7f7c38-1d4c-4e0b-9d0e-4c1f1d2c7a12"),
            {
                PROP_FORMAT: HAP_FORMAT_BOOL,
                PROP_PERMISSIONS: [HAP_PERMISSION_WRITE],
                PROP_ADMIN_ONLY_ACCESS: ["read"],
            },
        ),
        Characteristic(
            "Locked",
            UUID("2d7f7c38-1d4c-4e0b-9d0e-4c1f1d2c7a13"),
            {
                PROP_FORMAT: HAP_FORMAT_BOOL,
                PROP_PERMISSIONS: [HAP_PERMISSION_READ],
                PROP_ADMIN_ONLY_ACCESS: ["write"],
            },
        ),
    )
    acc.add_service(service)
    driver.add_accessory(acc)
    driver.pair(str(ADMIN).encode(), b"admin-key", HAP_PERMISSIONS.ADMIN)
    driver.pair(str(USER).encode(), b"user-key", HAP_PERMISSIONS.USER)
    with patch.object(driver, "publish"), patch.object(driver, "async_persist"):
        yield {
            "MotionEnabled": _with_setter(acc, service, "MotionEnabled"),
            "Open": _with_setter(acc, service, "Open"),
            "Secret": _with_setter(acc, service, "Secret"),
            "Sealed": _with_setter(acc, service, "Sealed"),
            "Locked": _with_setter(acc, service, "Locked"),
        }


@pytest.mark.parametrize(
    ("name", "client_uuid", "status", "calls"),
    [
        ("MotionEnabled", ADMIN, None, 1),
        ("MotionEnabled", USER, REFUSED, 0),
        ("MotionEnabled", UNPAIRED, REFUSED, 0),
        ("MotionEnabled", None, REFUSED, 0),
        ("Open", USER, None, 1),
        ("Open", None, None, 1),
    ],
    ids=[
        "admin",
        "user",
        "unpaired",
        "unknown_controller",
        "unrestricted_user",
        "unrestricted_unknown_controller",
    ],
)
def test_admin_only_write(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    name: str,
    client_uuid: UUID | None,
    status: int | None,
    calls: int,
) -> None:
    """Test only admins may write a characteristic restricted to them."""
    char = chars[name]

    response = driver.set_characteristics(_write(char), CLIENT, client_uuid=client_uuid)

    assert response == _response(char, status)
    assert char.setter.call_count == calls


def _demote(driver: AccessoryDriver) -> None:
    driver.pair(str(ADMIN).encode(), b"admin-key", HAP_PERMISSIONS.USER)


def _remove(driver: AccessoryDriver) -> None:
    driver.unpair(ADMIN)


@pytest.mark.parametrize("change", [_demote, _remove], ids=["demoted", "removed"])
def test_admin_change_applies_to_open_session(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    change: Callable[[AccessoryDriver], None],
) -> None:
    """Test a controller that loses admin is refused on its next write."""
    driver.pair(str(OTHER_ADMIN).encode(), b"other-key", HAP_PERMISSIONS.ADMIN)
    char = chars["MotionEnabled"]
    assert driver.set_characteristics(_write(char), CLIENT, client_uuid=ADMIN) is None

    change(driver)

    assert driver.set_characteristics(
        _write(char), CLIENT, client_uuid=ADMIN
    ) == _response(char, REFUSED)
    char.setter.assert_called_once_with(True)


def test_legacy_state_pairs_everyone_as_admin(
    driver: AccessoryDriver, chars: dict[str, SensorChar]
) -> None:
    """Test controllers from a state file without permissions are admins."""
    stored = io.StringIO()
    AccessoryEncoder.persist(stored, driver.state)
    legacy = json.loads(stored.getvalue())
    del legacy["client_properties"]
    AccessoryEncoder.load_into(io.StringIO(json.dumps(legacy)), driver.state)
    char = chars["MotionEnabled"]

    assert driver.set_characteristics(_write(char), CLIENT, client_uuid=USER) is None
    char.setter.assert_called_once_with(True)


@pytest.mark.parametrize(
    ("name", "client_uuid", "expected"),
    [
        ("Secret", ADMIN, {HAP_REPR_STATUS: 0, HAP_REPR_VALUE: False}),
        ("Secret", USER, {HAP_REPR_STATUS: REFUSED}),
        ("Secret", None, {HAP_REPR_STATUS: REFUSED}),
        ("MotionEnabled", USER, {HAP_REPR_STATUS: 0, HAP_REPR_VALUE: False}),
        ("MotionEnabled", None, {HAP_REPR_STATUS: 0, HAP_REPR_VALUE: False}),
    ],
    ids=[
        "admin",
        "user",
        "unknown_controller",
        "write_restricted_user",
        "write_restricted_unknown_controller",
    ],
)
def test_admin_only_read(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    name: str,
    client_uuid: UUID | None,
    expected: dict[str, Any],
) -> None:
    """Test reads are only refused when the restriction lists read."""
    char = chars[name]

    response = driver.get_characteristics(
        [f"{char.aid}.{char.iid}"], client_uuid=client_uuid
    )

    assert response == {
        HAP_REPR_CHARS: [{HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, **expected}]
    }


@pytest.mark.parametrize(
    ("client_uuid", "status"),
    [
        (USER, REFUSED),
        (ADMIN, HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC),
    ],
    ids=["user", "admin"],
)
def test_admin_only_write_refused_before_read_only(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    client_uuid: UUID,
    status: int,
) -> None:
    """Test a write to a read only admin char is refused for privileges first."""
    char = chars["Locked"]

    response = driver.set_characteristics(_write(char), CLIENT, client_uuid=client_uuid)

    assert response == _response(char, status)
    char.setter.assert_not_called()


@pytest.mark.parametrize(
    ("client_uuid", "status"),
    [
        (USER, REFUSED),
        (ADMIN, HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC),
    ],
    ids=["user", "admin"],
)
def test_admin_only_read_refused_before_write_only(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    client_uuid: UUID,
    status: int,
) -> None:
    """Test a read of a write only admin char is refused for privileges first."""
    char = chars["Sealed"]

    response = driver.get_characteristics(
        [f"{char.aid}.{char.iid}"], CLIENT, client_uuid=client_uuid
    )

    assert response == {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_STATUS: status}
        ]
    }


@pytest.mark.parametrize(
    ("client_uuid", "status"),
    [
        (USER, REFUSED),
        (ADMIN, HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE),
    ],
    ids=["user", "admin"],
)
def test_admin_only_read_refused_before_availability(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    client_uuid: UUID,
    status: int,
) -> None:
    """Test a read of an unavailable admin char is refused for privileges first."""
    char = chars["Secret"]

    with patch.object(
        Accessory, "available", new_callable=PropertyMock, return_value=False
    ):
        response = driver.get_characteristics(
            [f"{char.aid}.{char.iid}"], CLIENT, client_uuid=client_uuid
        )

    assert response == {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_STATUS: status}
        ]
    }


@pytest.mark.parametrize(
    ("client_uuid", "status_code", "body"),
    [
        (ADMIN, 204, b""),
        (USER, 207, b'{"characteristics":[{"status":-70401,"aid":1,"iid":10}]}'),
    ],
    ids=["admin", "user"],
)
def test_handler_passes_the_controller_on_write(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    client_uuid: UUID,
    status_code: int,
    body: bytes,
) -> None:
    """Test the HAP handler writes as the controller of its session."""
    char = chars["MotionEnabled"]
    handler = hap_handler.HAPServerHandler(driver, CLIENT)
    handler.is_encrypted = True
    handler.client_uuid = client_uuid
    handler.response = hap_handler.HAPResponse()
    handler.request_body = json.dumps(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_VALUE: True}
            ]
        }
    ).encode()

    handler.handle_set_characteristics()

    assert handler.response.status_code == status_code
    assert handler.response.body == body


@pytest.mark.parametrize(
    ("client_uuid", "status_code", "body"),
    [
        (ADMIN, 200, b'{"characteristics":[{"aid":1,"iid":12,"value":false}]}'),
        (USER, 207, b'{"characteristics":[{"aid":1,"iid":12,"status":-70401}]}'),
    ],
    ids=["admin", "user"],
)
def test_handler_passes_the_controller_on_read(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    client_uuid: UUID,
    status_code: int,
    body: bytes,
) -> None:
    """Test the HAP handler reads as the controller of its session."""
    char = chars["Secret"]
    handler = hap_handler.HAPServerHandler(driver, CLIENT)
    handler.is_encrypted = True
    handler.client_uuid = client_uuid
    handler.response = hap_handler.HAPResponse()
    handler.parsed_url = urlparse(f"/characteristics?id={char.aid}.{char.iid}")

    handler.handle_get_characteristics()

    assert handler.response.status_code == status_code
    assert handler.response.body == body
