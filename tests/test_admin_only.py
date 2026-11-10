"""Tests for characteristics restricted to admin controllers."""

from collections.abc import Callable, Iterator
import io
import json
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse
from uuid import UUID

import pytest

from pyhap import hap_handler
from pyhap.accessory import Accessory, get_topic
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
    HAP_PERMISSION_NOTIFY,
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
    characteristic whose restriction lists read, which is not enforced. Open is
    a made-up characteristic anyone may read and write. Locked is made up, read
    only and writable only by admins.
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


@pytest.mark.parametrize("name", ["Secret", "MotionEnabled"])
@pytest.mark.parametrize("client_uuid", [ADMIN, USER], ids=["admin", "user"])
def test_admin_only_read_not_refused(
    driver: AccessoryDriver,
    chars: dict[str, SensorChar],
    name: str,
    client_uuid: UUID,
) -> None:
    """Test reads are not restricted, also not when the restriction lists read."""
    char = chars[name]
    handler = hap_handler.HAPServerHandler(driver, CLIENT)
    handler.is_encrypted = True
    handler.client_uuid = client_uuid
    handler.response = hap_handler.HAPResponse()
    handler.parsed_url = urlparse(f"/characteristics?id={char.aid}.{char.iid}")

    handler.handle_get_characteristics()

    assert handler.response.status_code == 200
    assert json.loads(handler.response.body) == {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_VALUE: False}
        ]
    }


@pytest.mark.parametrize(
    "client_uuid", [ADMIN, USER, None], ids=["admin", "user", "unknown"]
)
def test_admin_only_event_subscription_allowed(
    driver: AccessoryDriver, chars: dict[str, SensorChar], client_uuid: UUID | None
) -> None:
    """Test a write without a value only subscribes, whoever sends it."""
    char = chars["MotionEnabled"]
    query = {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: char.aid,
                HAP_REPR_IID: char.iid,
                HAP_PERMISSION_NOTIFY: True,
            }
        ]
    }

    with patch.object(driver, "async_subscribe_client_topic") as subscribe:
        response = driver.set_characteristics(query, CLIENT, client_uuid=client_uuid)

    assert response is None
    subscribe.assert_called_once_with(CLIENT, get_topic(char.aid, char.iid), True)
    char.setter.assert_not_called()


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


def test_admin_only_null_write_reaches_no_setter(
    driver: AccessoryDriver, chars: dict[str, SensorChar]
) -> None:
    """Test an explicit null from a regular controller is refused before setters."""
    char = chars["MotionEnabled"]
    assert driver.accessory is not None
    service = driver.accessory.get_service("MotionSensor")
    assert service is not None
    service.setter_callback = MagicMock()
    driver.accessory.setter_callback = MagicMock()
    query = {
        HAP_REPR_CHARS: [
            {HAP_REPR_AID: char.aid, HAP_REPR_IID: char.iid, HAP_REPR_VALUE: None}
        ]
    }

    response = driver.set_characteristics(query, CLIENT, client_uuid=USER)

    assert response == _response(char, REFUSED)
    char.setter.assert_not_called()
    service.setter_callback.assert_not_called()
    driver.accessory.setter_callback.assert_not_called()


def _write_through_handler(
    driver: AccessoryDriver, char: SensorChar, client_uuid: UUID
) -> hap_handler.HAPResponse:
    """Write True to ``char`` through a HAP handler of ``client_uuid``."""
    handler = hap_handler.HAPServerHandler(driver, CLIENT)
    handler.is_encrypted = True
    handler.client_uuid = client_uuid
    handler.response = hap_handler.HAPResponse()
    handler.request_body = json.dumps(_write(char)).encode()
    handler.handle_set_characteristics()
    return handler.response


def test_handler_writes_as_admin(
    driver: AccessoryDriver, chars: dict[str, SensorChar]
) -> None:
    """Test the HAP handler writes as the admin controller of its session."""
    char = chars["MotionEnabled"]

    response = _write_through_handler(driver, char, ADMIN)

    assert response.status_code == 204
    assert response.body == b""
    char.setter.assert_called_once_with(True)


def test_handler_writes_as_user(
    driver: AccessoryDriver, chars: dict[str, SensorChar]
) -> None:
    """Test the HAP handler writes as the regular controller of its session."""
    char = chars["MotionEnabled"]

    response = _write_through_handler(driver, char, USER)

    assert response.status_code == 207
    assert json.loads(response.body) == _response(char, REFUSED)
    char.setter.assert_not_called()
