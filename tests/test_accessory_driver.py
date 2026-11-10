"""Tests for pyhap.accessory_driver."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os
import tempfile
import time
from typing import Any, NoReturn
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid1

from cryptography.hazmat.primitives import serialization
import pytest
from zeroconf import InterfaceChoice

from pyhap import util
from pyhap.accessory import STANDALONE_AID, Accessory, Bridge
from pyhap.accessory_driver import (
    MAX_FAILED_PAIRING_ATTEMPTS,
    MAX_PREPARED_WRITE_TTL_SECONDS,
    MAX_PREPARED_WRITES_PER_CLIENT,
    PAIRING_LOCKOUT_SECONDS,
    AccessoryDriver,
    AccessoryMDNSServiceInfo,
)
from pyhap.characteristic import (
    HAP_FORMAT_INT,
    HAP_PERMISSION_READ,
    PROP_FORMAT,
    PROP_PERMISSIONS,
    Characteristic,
)
from pyhap.const import (
    HAP_PERMISSION_NOTIFY,
    HAP_PERMISSION_WRITE,
    HAP_PERMISSIONS,
    HAP_REPR_ACCS,
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_PID,
    HAP_REPR_SERVICES,
    HAP_REPR_STATUS,
    HAP_REPR_TTL,
    HAP_REPR_VALUE,
    HAP_REPR_WRITE_RESPONSE,
    HAP_SERVER_STATUS,
)
from pyhap.hap_crypto import hap_hkdf
from pyhap.hap_protocol import HAPServerProtocol
from pyhap.service import Service
from pyhap.state import State

CHAR_PROPS = {
    PROP_FORMAT: HAP_FORMAT_INT,
    PROP_PERMISSIONS: [HAP_PERMISSION_READ, HAP_PERMISSION_WRITE],
}


class AsyncIntervalAccessory(Accessory):
    """An accessory increments a counter at interval."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._counter = 0

    @Accessory.run_at_interval(0.001)  # Run this method every 0.001 seconds
    async def run(self) -> None:
        self._counter += 1

    @property
    def counter(self) -> int:
        return self._counter


class SyncIntervalAccessory(Accessory):
    """An accessory increments a counter at interval."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._counter = 0

    @Accessory.run_at_interval(0.001)  # Run this method every 0.001 seconds
    def run(self) -> None:
        self._counter += 1

    @property
    def counter(self) -> int:
        return self._counter


class UnavailableAccessory(Accessory):
    """An accessory that is not available."""

    @property
    def available(self) -> bool:
        return False


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize("available", [False, True], ids=["unavailable", "available"])
def test_get_characteristics_honors_availability(
    driver: AccessoryDriver, bridged: bool, available: bool
) -> None:
    """Respect accessory availability for standalone and bridged reads."""
    accessory_type = Accessory if available else UnavailableAccessory
    acc = accessory_type(driver, "Test Accessory", aid=2 if bridged else STANDALONE_AID)
    service = Service(uuid1(), "Test Service")
    char = Characteristic("Value", uuid1(), CHAR_PROPS)
    getter = MagicMock(return_value=42)
    char.getter_callback = getter
    service.add_characteristic(char)
    acc.add_service(service)
    if bridged:
        bridge = Bridge(driver, "Test Bridge")
        bridge.add_accessory(acc)
        driver.add_accessory(bridge)
    else:
        driver.add_accessory(acc)
    iid = acc.iid_manager.get_iid(char)

    result = driver.get_characteristics([f"{acc.aid}.{iid}"])

    expected = {
        HAP_REPR_AID: acc.aid,
        HAP_REPR_IID: iid,
        HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
    }
    if available:
        expected[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SUCCESS
        expected[HAP_REPR_VALUE] = 42
        getter.assert_called_once_with()
    else:
        getter.assert_not_called()
    assert result == {HAP_REPR_CHARS: [expected]}

    if not available:
        with patch.object(UnavailableAccessory, "available", True):
            expected[HAP_REPR_STATUS] = HAP_SERVER_STATUS.SUCCESS
            expected[HAP_REPR_VALUE] = 42
            assert driver.get_characteristics([f"{acc.aid}.{iid}"]) == {
                HAP_REPR_CHARS: [expected]
            }
        getter.assert_called_once_with()


def _add_bridged_or_standalone(
    driver: AccessoryDriver, permissions: list[str], bridged: bool
) -> tuple[Accessory, Characteristic, MagicMock]:
    """Add an accessory with one int characteristic that has a getter."""
    acc = Accessory(driver, "Test Accessory", aid=2 if bridged else STANDALONE_AID)
    service = Service(uuid1(), "Test Service")
    char = Characteristic(
        "Value",
        uuid1(),
        {PROP_FORMAT: HAP_FORMAT_INT, PROP_PERMISSIONS: permissions},
    )
    getter = MagicMock(return_value=42)
    char.getter_callback = getter
    service.add_characteristic(char)
    acc.add_service(service)
    if bridged:
        bridge = Bridge(driver, "Test Bridge")
        bridge.add_accessory(acc)
        driver.add_accessory(bridge)
    else:
        driver.add_accessory(acc)
    return acc, char, getter


READ_PERMISSION_CASES = [
    pytest.param(
        ["pr"],
        {HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS, HAP_REPR_VALUE: 42},
        1,
        id="read",
    ),
    pytest.param(
        ["pr", "pw", "ev"],
        {HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS, HAP_REPR_VALUE: 42},
        1,
        id="read-write-notify",
    ),
    pytest.param(
        ["pw"],
        {HAP_REPR_STATUS: HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC},
        0,
        id="write-only",
    ),
    pytest.param(
        ["pw", "ev"],
        {HAP_REPR_STATUS: HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC},
        0,
        id="write-notify",
    ),
    pytest.param(
        ["pw", "tw"],
        {HAP_REPR_STATUS: HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC},
        0,
        id="timed-write-only",
    ),
]


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize(("permissions", "expected", "reads"), READ_PERMISSION_CASES)
def test_get_characteristics_requires_read_permission(
    driver: AccessoryDriver,
    bridged: bool,
    permissions: list[str],
    expected: dict[str, Any],
    reads: int,
) -> None:
    """A read of a characteristic without paired read returns no value."""
    acc, char, getter = _add_bridged_or_standalone(driver, permissions, bridged)
    iid = acc.iid_manager.get_iid(char)

    result = driver.get_characteristics([f"{acc.aid}.{iid}"])

    assert result == {
        HAP_REPR_CHARS: [{HAP_REPR_AID: acc.aid, HAP_REPR_IID: iid, **expected}]
    }
    assert getter.call_count == reads


@pytest.mark.parametrize(
    ("permissions", "values", "reads"),
    [
        (["pr"], {HAP_REPR_VALUE}, 1),
        (["pr", "pw", "ev"], {HAP_REPR_VALUE}, 1),
        (["pw"], set(), 0),
        (["pw", "ev"], set(), 0),
        (["pw", "tw"], set(), 0),
    ],
)
def test_get_accessories_value_requires_read_permission(
    driver: AccessoryDriver, permissions: list[str], values: set[str], reads: int
) -> None:
    """The accessory database only includes values the controller may read."""
    acc, char, getter = _add_bridged_or_standalone(driver, permissions, False)

    (acc_rep,) = driver.get_accessories()[HAP_REPR_ACCS]
    (char_rep,) = acc_rep[HAP_REPR_SERVICES][-1][HAP_REPR_CHARS]

    assert char_rep[HAP_REPR_IID] == acc.iid_manager.get_iid(char)
    assert char_rep.keys() & {HAP_REPR_VALUE} == values
    assert getter.call_count == reads


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize(
    "permissions", [["pr", "ev"], ["pr", "pw", "ev"], ["pw", "ev"]]
)
def test_subscribe_with_notify_permission(
    driver: AccessoryDriver, bridged: bool, permissions: list[str]
) -> None:
    """Characteristics with the notify permission can be (un)subscribed."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, bridged)
    iid = acc.iid_manager.get_iid(char)
    client = ("1.2.3.4", 5)

    subscribe = {HAP_REPR_AID: acc.aid, HAP_REPR_IID: iid, HAP_PERMISSION_NOTIFY: True}
    assert driver.set_characteristics({HAP_REPR_CHARS: [subscribe]}, client) is None
    assert driver.topics == {f"{acc.aid}.{iid}": {client}}

    unsubscribe = {**subscribe, HAP_PERMISSION_NOTIFY: False}
    assert driver.set_characteristics({HAP_REPR_CHARS: [unsubscribe]}, client) is None
    assert driver.topics == {}


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize("ev", [True, False], ids=["subscribe", "unsubscribe"])
@pytest.mark.parametrize("permissions", [["pr"], ["pr", "pw"], ["pw"], ["pw", "tw"]])
def test_subscribe_without_notify_permission(
    driver: AccessoryDriver, bridged: bool, ev: bool, permissions: list[str]
) -> None:
    """(Un)subscribing a characteristic without notify answers -70406."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, bridged)
    iid = acc.iid_manager.get_iid(char)
    client = ("1.2.3.4", 5)

    result = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: acc.aid, HAP_REPR_IID: iid, HAP_PERMISSION_NOTIFY: ev}
            ]
        },
        client,
    )

    assert result == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.NOTIFICATION_NOT_SUPPORTED,
            }
        ]
    }
    assert driver.topics == {}


def test_refused_subscription_lists_the_others(driver: AccessoryDriver) -> None:
    """A 207 for a refused subscription also lists the accepted ones."""
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID)
    service = Service(uuid1(), "Test Service")
    notifiable = Characteristic(
        "Notifiable",
        uuid1(),
        {PROP_FORMAT: HAP_FORMAT_INT, PROP_PERMISSIONS: ["pr", "ev"]},
    )
    silent = Characteristic(
        "Silent", uuid1(), {PROP_FORMAT: HAP_FORMAT_INT, PROP_PERMISSIONS: ["pr"]}
    )
    service.add_characteristic(notifiable)
    service.add_characteristic(silent)
    acc.add_service(service)
    driver.add_accessory(acc)
    ok_iid = acc.iid_manager.get_iid(notifiable)
    refused_iid = acc.iid_manager.get_iid(silent)
    client = ("1.2.3.4", 5)

    result = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: ok_iid,
                    HAP_PERMISSION_NOTIFY: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: refused_iid,
                    HAP_PERMISSION_NOTIFY: True,
                },
            ]
        },
        client,
    )

    assert result == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: refused_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.NOTIFICATION_NOT_SUPPORTED,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: ok_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS,
            },
        ]
    }
    assert driver.topics == {f"{acc.aid}.{ok_iid}": {client}}


@pytest.mark.parametrize("ev", [True, False], ids=["subscribe", "unsubscribe"])
def test_write_with_subscribe_without_notify_permission(
    driver: AccessoryDriver, ev: bool
) -> None:
    """A write combined with a refused subscription is not applied."""
    acc, char, _ = _add_bridged_or_standalone(driver, ["pr", "pw"], False)
    iid = acc.iid_manager.get_iid(char)
    setter = MagicMock()
    char.setter_callback = setter

    result = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: iid,
                    HAP_PERMISSION_NOTIFY: ev,
                    HAP_REPR_VALUE: 5,
                }
            ]
        },
        ("1.2.3.4", 5),
    )

    assert result == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.NOTIFICATION_NOT_SUPPORTED,
            }
        ]
    }
    setter.assert_not_called()
    assert driver.topics == {}


def _write_value(
    driver: AccessoryDriver, acc: Accessory, char: Characteristic, **query: Any
) -> tuple[dict[str, Any] | None, MagicMock, MagicMock]:
    """Write 5 to ``char`` and return the result and both setter mocks."""
    setter = MagicMock()
    char.setter_callback = setter
    service_setter = MagicMock()
    assert char.service is not None
    char.service.setter_callback = service_setter
    iid = acc.iid_manager.get_iid(char)
    result = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: acc.aid, HAP_REPR_IID: iid, HAP_REPR_VALUE: 5, **query}
            ]
        },
        ("1.2.3.4", 5),
    )
    return result, setter, service_setter


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize("permissions", [["pr"], ["pr", "ev"]])
def test_write_without_write_permission(
    driver: AccessoryDriver, bridged: bool, permissions: list[str]
) -> None:
    """A write to a characteristic without paired write is refused."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, bridged)

    result, setter, service_setter = _write_value(driver, acc, char)

    assert result == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: acc.iid_manager.get_iid(char),
                HAP_REPR_STATUS: HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC,
            }
        ]
    }
    setter.assert_not_called()
    service_setter.assert_not_called()
    assert char.value == 0


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize("permissions", [["pw"], ["pr", "pw"], ["pr", "pw", "ev"]])
def test_write_with_write_permission(
    driver: AccessoryDriver, bridged: bool, permissions: list[str]
) -> None:
    """A write to a characteristic with paired write is applied."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, bridged)

    result, setter, service_setter = _write_value(driver, acc, char)

    assert result is None
    setter.assert_called_once_with(5)
    service_setter.assert_called_once_with({char.display_name: 5})
    assert char.value == 5


def test_write_with_subscribe_without_write_permission(
    driver: AccessoryDriver,
) -> None:
    """A subscription is applied even when the write in the same entry is not."""
    acc, char, _ = _add_bridged_or_standalone(driver, ["pr", "ev"], False)
    client = ("1.2.3.4", 5)
    iid = acc.iid_manager.get_iid(char)

    result, setter, _ = _write_value(driver, acc, char, **{HAP_PERMISSION_NOTIFY: True})

    assert result == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC,
            }
        ]
    }
    setter.assert_not_called()
    assert driver.topics == {f"{acc.aid}.{iid}": {client}}


def test_refused_write_does_not_block_others(driver: AccessoryDriver) -> None:
    """A refused write leaves the other characteristics of the request alone."""
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID)
    service = Service(uuid1(), "Test Service")
    read_only = Characteristic(
        "ReadOnly", uuid1(), {PROP_FORMAT: HAP_FORMAT_INT, PROP_PERMISSIONS: ["pr"]}
    )
    writable = Characteristic("Writable", uuid1(), CHAR_PROPS)
    service.add_characteristic(read_only, writable)
    acc.add_service(service)
    driver.add_accessory(acc)
    setter = MagicMock()
    writable.setter_callback = setter
    read_only_iid = acc.iid_manager.get_iid(read_only)
    writable_iid = acc.iid_manager.get_iid(writable)

    result = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {HAP_REPR_AID: acc.aid, HAP_REPR_IID: read_only_iid, HAP_REPR_VALUE: 5},
                {HAP_REPR_AID: acc.aid, HAP_REPR_IID: writable_iid, HAP_REPR_VALUE: 5},
            ]
        },
        ("1.2.3.4", 5),
    )

    assert result == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: read_only_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.READ_ONLY_CHARACTERISTIC,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: writable_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS,
            },
        ]
    }
    setter.assert_called_once_with(5)
    assert read_only.value == 0
    assert writable.value == 5


def test_auto_add_aid_mac(driver: AccessoryDriver) -> None:
    acc = Accessory(driver, "Test Accessory")
    driver.add_accessory(acc)
    assert acc.aid == STANDALONE_AID
    assert driver.state.mac is not None


def test_not_standalone_aid(driver: AccessoryDriver) -> None:
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID + 1)
    with pytest.raises(ValueError):
        driver.add_accessory(acc)


def test_persist_load(async_zeroconf: Any) -> None:
    with tempfile.NamedTemporaryFile(mode="r+") as file:
        with patch("pyhap.accessory_driver.HAPServer"):
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.persist()
            pk = driver.state.public_key
            # Re-start driver with a "new" accessory. State gets loaded into
            # the new accessory.
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.load()
    assert driver.state.public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    ) == pk.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def test_persist_cannot_write(async_zeroconf: Any) -> None:
    with tempfile.NamedTemporaryFile(mode="r+") as file:
        with patch("pyhap.accessory_driver.HAPServer"):
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.persist_file = "/file/that/will/not/exist"
            with pytest.raises(OSError):
                driver.persist()


def test_external_zeroconf() -> None:
    zeroconf = MagicMock()
    with (
        patch("pyhap.accessory_driver.HAPServer"),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
    ):
        driver = AccessoryDriver(port=51234, async_zeroconf_instance=zeroconf)
    assert driver.advertiser == zeroconf


def test_advertised_address() -> None:
    zeroconf = MagicMock()
    with (
        patch("pyhap.accessory_driver.HAPServer"),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
    ):
        driver = AccessoryDriver(
            port=51234,
            async_zeroconf_instance=zeroconf,
            advertised_address=["1.2.3.4", "::1"],
        )
    assert driver.advertiser == zeroconf
    assert driver.state.addresses == ["1.2.3.4", "::1"]


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"address": "1.2.3.4"}, "1.2.3.4"),
        (
            {"address": "1.2.3.4", "listen_address": ["0.0.0.0", "::"]},
            ["0.0.0.0", "::"],
        ),
    ],
)
def test_listen_address(kwargs: dict[str, Any], expected: Any) -> None:
    with (
        patch("pyhap.accessory_driver.HAPServer") as server,
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
    ):
        driver = AccessoryDriver(
            port=51234, async_zeroconf_instance=MagicMock(), **kwargs
        )
    assert driver.listen_address() == expected
    assert server.call_args.args[0] == (expected, 51234)


WRITER = ("1.2.3.4", 5)
OTHER_CONTROLLER = ("5.6.7.8", 9)


def _write(
    driver: AccessoryDriver, acc: Accessory, char: Characteristic, **query: Any
) -> dict[str, Any] | None:
    """Write 0 to ``char`` from a mock client with extra query fields."""
    return driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: acc.iid_manager.get_iid(char),
                    HAP_REPR_VALUE: 0,
                    **query,
                }
            ]
        },
        WRITER,
    )


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize(
    ("permissions", "stored"),
    [
        # A readable char keeps its default value, see the read tests below.
        pytest.param(["pr", "pw", "wr"], 0, id="readable"),
        pytest.param(["pw", "wr"], 1, id="write-only"),
    ],
)
@pytest.mark.parametrize(("returned", "sent"), [(1, 1), (1.7, 1)])
def test_write_response_returned_when_requested(
    driver: AccessoryDriver,
    bridged: bool,
    permissions: list[str],
    stored: int,
    returned: Any,
    sent: int,
) -> None:
    """A write response carries the setter's value as the char stores it."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, bridged)
    service_setter = MagicMock()
    assert char.service is not None
    char.service.setter_callback = service_setter
    char.setter_callback = MagicMock(return_value=returned)

    response = _write(driver, acc, char, **{HAP_REPR_WRITE_RESPONSE: True})

    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: acc.iid_manager.get_iid(char),
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS,
                HAP_REPR_VALUE: sent,
            }
        ]
    }
    char.setter_callback.assert_called_once_with(0)
    service_setter.assert_called_once_with({"Value": 0})
    assert char.value == stored


@pytest.mark.parametrize(
    ("permissions", "query"),
    [
        pytest.param(["pr", "pw", "wr"], {}, id="not-requested"),
        pytest.param(
            ["pr", "pw", "wr"], {HAP_REPR_WRITE_RESPONSE: False}, id="declined"
        ),
        pytest.param(
            ["pr", "pw"], {HAP_REPR_WRITE_RESPONSE: True}, id="no-wr-permission"
        ),
        pytest.param(["pw"], {HAP_REPR_WRITE_RESPONSE: True}, id="write-only"),
    ],
)
def test_write_response_not_returned(
    driver: AccessoryDriver, permissions: list[str], query: dict[str, Any]
) -> None:
    """Without a request or the wr permission the write has no response body."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, False)
    char.setter_callback = MagicMock(return_value=1)

    assert _write(driver, acc, char, **query) is None
    char.setter_callback.assert_called_once_with(0)
    assert char.value == 0


@pytest.mark.parametrize(
    ("permissions", "stored"),
    [
        pytest.param(["pr", "pw", "wr"], 0, id="readable"),
        pytest.param(["pw", "wr"], 5, id="write-only"),
    ],
)
def test_invalid_write_response_is_sent_but_not_kept(
    driver: AccessoryDriver, permissions: list[str], stored: int
) -> None:
    """A response the char can't hold is still sent, but not kept as its value."""
    acc, char, _ = _add_bridged_or_standalone(driver, permissions, False)
    char.setter_callback = MagicMock(return_value="not a number")

    response = _write(
        driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True}
    )

    assert response is not None
    assert response[HAP_REPR_CHARS][0][HAP_REPR_VALUE] == "not a number"
    assert char.value == stored


def test_write_response_value_of_write_only_char_not_readable(
    driver: AccessoryDriver,
) -> None:
    """Neither the written nor the response value of a wr char without pr leaks."""
    acc, char, _ = _add_bridged_or_standalone(driver, ["pw", "wr"], False)
    char.setter_callback = MagicMock(return_value=1)
    iid = acc.iid_manager.get_iid(char)

    _write(driver, acc, char, **{HAP_REPR_WRITE_RESPONSE: True})

    assert driver.get_characteristics([f"{acc.aid}.{iid}"]) == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.WRITE_ONLY_CHARACTERISTIC,
            }
        ]
    }
    (acc_rep,) = driver.get_accessories()[HAP_REPR_ACCS]
    assert HAP_REPR_VALUE not in acc_rep[HAP_REPR_SERVICES][-1][HAP_REPR_CHARS][0]


def _add_write_response_char(
    driver: AccessoryDriver, bridged: bool
) -> tuple[Accessory, Characteristic, str]:
    """Add a pr, pw and wr char that answers a write with 7."""
    acc, char, _ = _add_bridged_or_standalone(driver, ["pr", "pw", "wr"], bridged)
    char.getter_callback = None
    char.setter_callback = MagicMock(return_value=7)
    return acc, char, f"{acc.aid}.{acc.iid_manager.get_iid(char)}"


def _read(driver: AccessoryDriver, char_id: str, client_addr: Any) -> Any:
    """Return the value ``client_addr`` reads from ``char_id``."""
    (rep,) = driver.get_characteristics([char_id], client_addr)[HAP_REPR_CHARS]
    assert rep[HAP_REPR_STATUS] == HAP_SERVER_STATUS.SUCCESS
    return rep[HAP_REPR_VALUE]


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
@pytest.mark.parametrize(
    ("reader", "expected"),
    [
        pytest.param(WRITER, 7, id="writer"),
        pytest.param(OTHER_CONTROLLER, 0, id="other-controller"),
        pytest.param(None, 0, id="no-client"),
    ],
)
def test_write_response_readable_only_by_writer(
    driver: AccessoryDriver, bridged: bool, reader: Any, expected: int
) -> None:
    """Only the controller that wrote reads its write response back."""
    acc, char, char_id = _add_write_response_char(driver, bridged)

    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    assert _read(driver, char_id, reader) == expected


def test_write_responses_kept_per_controller(driver: AccessoryDriver) -> None:
    """Each controller reads its own last write response."""
    acc, char, char_id = _add_write_response_char(driver, False)
    char.setter_callback = MagicMock(side_effect=[7, 8])
    request = {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: acc.iid_manager.get_iid(char),
                HAP_REPR_VALUE: 5,
                HAP_REPR_WRITE_RESPONSE: True,
            }
        ]
    }

    driver.set_characteristics(request, WRITER)
    driver.set_characteristics(request, OTHER_CONTROLLER)

    assert _read(driver, char_id, WRITER) == 7
    assert _read(driver, char_id, OTHER_CONTROLLER) == 8


@pytest.mark.parametrize("bridged", [False, True], ids=["standalone", "bridged"])
def test_write_response_not_in_accessories(
    driver: AccessoryDriver, bridged: bool
) -> None:
    """The accessory database has the default value, not the write response."""
    acc, char, _ = _add_write_response_char(driver, bridged)

    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    acc_rep = driver.get_accessories()[HAP_REPR_ACCS][-1]
    assert acc_rep[HAP_REPR_SERVICES][-1][HAP_REPR_CHARS][0][HAP_REPR_VALUE] == 0


async def test_write_response_dropped_on_disconnect(driver: AccessoryDriver) -> None:
    """A write response is not read back after the connection closed."""
    driver.loop = asyncio.get_running_loop()
    acc, char, char_id = _add_write_response_char(driver, False)
    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    driver.connection_lost(WRITER)
    assert _read(driver, char_id, WRITER) == 7
    # The response is dropped once the loop runs.
    await asyncio.sleep(0)

    assert _read(driver, char_id, WRITER) == 0


def test_plain_write_replaces_write_response(driver: AccessoryDriver) -> None:
    """A write without ``r`` drops the writer's former response."""
    acc, char, char_id = _add_write_response_char(driver, False)
    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    _write(driver, acc, char, **{HAP_REPR_VALUE: 9})

    assert _read(driver, char_id, WRITER) == 9
    assert _read(driver, char_id, OTHER_CONTROLLER) == 9


@pytest.mark.parametrize(
    ("setter_kwargs", "writer_reads"),
    [
        pytest.param({"return_value": 7}, 7, id="answered"),
        pytest.param({"side_effect": RuntimeError}, 3, id="raises"),
    ],
)
def test_write_response_keeps_accessory_value(
    driver: AccessoryDriver, setter_kwargs: dict[str, Any], writer_reads: int
) -> None:
    """Other connections keep reading the value the accessory set."""
    acc, char, char_id = _add_write_response_char(driver, False)
    char.setter_callback = MagicMock(**setter_kwargs)
    char.set_value(3)

    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    assert _read(driver, char_id, WRITER) == writer_reads
    assert _read(driver, char_id, OTHER_CONTROLLER) == 3


def test_write_response_keeps_value_set_by_setter(driver: AccessoryDriver) -> None:
    """A value the setter sets on its own characteristic is kept."""
    acc, char, char_id = _add_write_response_char(driver, False)

    def _setter(value: int) -> int:
        char.set_value(42)
        return 7

    char.setter_callback = _setter

    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    assert _read(driver, char_id, WRITER) == 7
    assert _read(driver, char_id, OTHER_CONTROLLER) == 42


@pytest.mark.parametrize(
    ("write_response", "notified"),
    [
        pytest.param(True, [], id="private"),
        pytest.param(False, [5], id="plain"),
    ],
)
def test_private_write_not_notified(
    driver: AccessoryDriver, write_response: bool, notified: list[int]
) -> None:
    """Subscribers are not told the request of a write with ``r``."""
    acc, char, _ = _add_bridged_or_standalone(driver, ["pr", "pw", "ev", "wr"], False)
    char.getter_callback = None
    char.setter_callback = MagicMock(return_value=7)
    published = []
    with patch.object(
        acc, "publish", side_effect=lambda value, *_: published.append(value)
    ):
        _write(
            driver,
            acc,
            char,
            **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: write_response},
        )

    assert published == notified


@pytest.mark.parametrize("reader", [WRITER, OTHER_CONTROLLER], ids=["writer", "other"])
@pytest.mark.parametrize(
    "make_setter",
    [
        pytest.param(partial(MagicMock, side_effect=RuntimeError), id="raises"),
        pytest.param(partial(MagicMock, return_value=None), id="no-response"),
        pytest.param(lambda: None, id="no-setter"),
    ],
)
def test_write_response_request_not_shared_without_response(
    driver: AccessoryDriver, reader: Any, make_setter: Any
) -> None:
    """The written request stays private when no response is kept."""
    acc, char, char_id = _add_write_response_char(driver, False)
    char.setter_callback = make_setter()

    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True})

    assert _read(driver, char_id, reader) == 0


@pytest.mark.parametrize(
    "second",
    [
        pytest.param(RuntimeError("boom"), id="raises"),
        pytest.param("not a number", id="invalid"),
        pytest.param(None, id="no-response"),
    ],
)
def test_stale_write_response_dropped(driver: AccessoryDriver, second: Any) -> None:
    """A later write that keeps no response drops the former one."""
    acc, char, char_id = _add_write_response_char(driver, False)
    char.setter_callback = MagicMock(side_effect=[7, second])
    query = {HAP_REPR_VALUE: 5, HAP_REPR_WRITE_RESPONSE: True}

    _write(driver, acc, char, **query)
    _write(driver, acc, char, **query)

    assert _read(driver, char_id, WRITER) == 0


def test_write_response_without_client_not_kept(driver: AccessoryDriver) -> None:
    """A write response without a known connection is sent but not kept."""
    acc, char, _ = _add_write_response_char(driver, False)
    request = {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: acc.iid_manager.get_iid(char),
                HAP_REPR_VALUE: 5,
                HAP_REPR_WRITE_RESPONSE: True,
            }
        ]
    }

    no_client: Any = None
    response = driver.set_characteristics(request, no_client)

    assert response is not None
    assert response[HAP_REPR_CHARS][0][HAP_REPR_VALUE] == 7
    assert driver._write_responses == {}


@pytest.mark.parametrize("reader", [WRITER, OTHER_CONTROLLER], ids=["writer", "other"])
@pytest.mark.parametrize(
    "query",
    [
        pytest.param({}, id="not-requested"),
        pytest.param({HAP_REPR_WRITE_RESPONSE: False}, id="declined"),
    ],
)
def test_written_value_readable_without_write_response(
    driver: AccessoryDriver, reader: Any, query: dict[str, Any]
) -> None:
    """Without a write response the written value is read by every controller."""
    acc, char, char_id = _add_write_response_char(driver, False)

    _write(driver, acc, char, **{HAP_REPR_VALUE: 5, **query})

    assert _read(driver, char_id, reader) == 5


def test_service_callbacks(driver: AccessoryDriver) -> None:
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    mock_callback = MagicMock()
    service.setter_callback = mock_callback

    acc.add_service(service)
    bridge.add_accessory(acc)

    service2 = Service(uuid1(), "Lightbulb")
    char_on2 = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness2 = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service2.add_characteristic(char_on2)
    service2.add_characteristic(char_brightness2)

    mock_callback2 = MagicMock()
    service2.setter_callback = mock_callback2

    acc2.add_service(service2)
    bridge.add_accessory(acc2)

    char_on_iid = char_on.to_HAP()[HAP_REPR_IID]
    char_brightness_iid = char_brightness.to_HAP()[HAP_REPR_IID]
    char_on2_iid = char_on2.to_HAP()[HAP_REPR_IID]
    char_brightness2_iid = char_brightness2.to_HAP()[HAP_REPR_IID]

    driver.add_accessory(bridge)

    response = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_brightness_iid,
                    HAP_REPR_VALUE: 88,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_on2_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_brightness2_iid,
                    HAP_REPR_VALUE: 12,
                },
            ]
        },
        "mock_addr",  # ty: ignore[invalid-argument-type]
    )
    assert response is None

    mock_callback2.assert_called_with({"On": True, "Brightness": 12})
    mock_callback.assert_called_with({"On": True, "Brightness": 88})

    get_chars = driver.get_characteristics(
        [f"{acc.aid}.{char_on_iid}", f"{acc2.aid}.{char_on2_iid}"]
    )
    assert get_chars == {
        "characteristics": [
            {"aid": acc.aid, "iid": char_on_iid, "status": 0, "value": True},
            {"aid": acc2.aid, "iid": char_on2_iid, "status": -70402},
        ]
    }

    def _fail_func() -> NoReturn:
        raise ValueError

    char_brightness.getter_callback = _fail_func
    get_chars = driver.get_characteristics(
        [
            f"{acc.aid}.{char_on_iid}",
            f"{acc2.aid}.{char_on2_iid}",
            f"{acc2.aid}.{char_brightness_iid}",
            f"{acc.aid}.{char_brightness2_iid}",
        ]
    )
    assert get_chars == {
        "characteristics": [
            {"aid": acc.aid, "iid": char_on_iid, "status": 0, "value": True},
            {"aid": acc2.aid, "iid": char_on2_iid, "status": -70402},
            {"aid": acc2.aid, "iid": char_brightness2_iid, "status": -70402},
            {"aid": acc.aid, "iid": char_brightness_iid, "status": -70402},
        ]
    }


def test_service_callbacks_partial_failure(driver: AccessoryDriver) -> None:
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    def fail_callback(*_: Any) -> NoReturn:
        raise ValueError

    service.setter_callback = fail_callback

    acc.add_service(service)
    bridge.add_accessory(acc)

    service2 = Service(uuid1(), "Lightbulb")
    char_on2 = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness2 = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service2.add_characteristic(char_on2)
    service2.add_characteristic(char_brightness2)

    mock_callback2 = MagicMock()
    service2.setter_callback = mock_callback2

    acc2.add_service(service2)
    bridge.add_accessory(acc2)

    char_on_iid = char_on.to_HAP()[HAP_REPR_IID]
    char_brightness_iid = char_brightness.to_HAP()[HAP_REPR_IID]
    char_on2_iid = char_on2.to_HAP()[HAP_REPR_IID]
    char_brightness2_iid = char_brightness2.to_HAP()[HAP_REPR_IID]

    driver.add_accessory(bridge)

    response = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_brightness_iid,
                    HAP_REPR_VALUE: 88,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_on2_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_brightness2_iid,
                    HAP_REPR_VALUE: 12,
                },
            ]
        },
        "mock_addr",  # ty: ignore[invalid-argument-type]
    )

    mock_callback2.assert_called_with({"On": True, "Brightness": 12})
    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_on_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_brightness_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_on2_iid,
                HAP_REPR_STATUS: 0,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_brightness2_iid,
                HAP_REPR_STATUS: 0,
            },
        ]
    }


def test_mixing_service_char_callbacks_partial_failure(driver: AccessoryDriver) -> None:
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    def fail_callback(*_: Any) -> NoReturn:
        raise ValueError

    service.setter_callback = fail_callback

    acc.add_service(service)
    bridge.add_accessory(acc)

    service2 = Service(uuid1(), "Lightbulb")
    char_on2 = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness2 = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service2.add_characteristic(char_on2)
    service2.add_characteristic(char_brightness2)

    char_on2.setter_callback = fail_callback

    acc2.add_service(service2)
    bridge.add_accessory(acc2)

    char_on_iid = char_on.to_HAP()[HAP_REPR_IID]
    char_brightness_iid = char_brightness.to_HAP()[HAP_REPR_IID]
    char_on2_iid = char_on2.to_HAP()[HAP_REPR_IID]
    char_brightness2_iid = char_brightness2.to_HAP()[HAP_REPR_IID]

    driver.add_accessory(bridge)

    response = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_brightness_iid,
                    HAP_REPR_VALUE: 88,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_on2_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_brightness2_iid,
                    HAP_REPR_VALUE: 12,
                },
            ]
        },
        "mock_addr",  # ty: ignore[invalid-argument-type]
    )

    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_on_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_brightness_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_on2_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_brightness2_iid,
                HAP_REPR_STATUS: 0,
            },
        ]
    }


def test_start_from_sync(driver: AccessoryDriver) -> None:
    """Start from sync."""

    class Acc(Accessory):
        @Accessory.run_at_interval(0)
        async def run(self) -> None:
            driver.executor = ThreadPoolExecutor()
            driver.loop.set_default_executor(driver.executor)
            await driver.async_stop()

        def setup_message(self) -> None:
            pass

    acc = Acc(driver, "TestAcc")
    driver.add_accessory(acc)
    with patch.object(driver.loop, "close"):
        driver.start()


def test_accessory_level_callbacks(driver: AccessoryDriver) -> None:
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    switch_service = Service(uuid1(), "Switch")
    char_switch_on = Characteristic("On", uuid1(), CHAR_PROPS)
    switch_service.add_characteristic(char_switch_on)

    mock_callback = MagicMock()
    acc.setter_callback = mock_callback

    acc.add_service(service)
    acc.add_service(switch_service)
    bridge.add_accessory(acc)

    service2 = Service(uuid1(), "Lightbulb")
    char_on2 = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness2 = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service2.add_characteristic(char_on2)
    service2.add_characteristic(char_brightness2)

    mock_callback2 = MagicMock()
    acc2.setter_callback = mock_callback2

    acc2.add_service(service2)
    bridge.add_accessory(acc2)

    char_switch_on_iid = char_switch_on.to_HAP()[HAP_REPR_IID]
    char_on_iid = char_on.to_HAP()[HAP_REPR_IID]
    char_brightness_iid = char_brightness.to_HAP()[HAP_REPR_IID]
    char_on2_iid = char_on2.to_HAP()[HAP_REPR_IID]
    char_brightness2_iid = char_brightness2.to_HAP()[HAP_REPR_IID]

    driver.add_accessory(bridge)

    response = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_switch_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_brightness_iid,
                    HAP_REPR_VALUE: 88,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_on2_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_brightness2_iid,
                    HAP_REPR_VALUE: 12,
                },
            ]
        },
        "mock_addr",  # ty: ignore[invalid-argument-type]
    )
    assert response is None

    mock_callback.assert_called_with(
        {
            service: {char_on: True, char_brightness: 88},
            switch_service: {char_switch_on: True},
        }
    )
    mock_callback2.assert_called_with(
        {service2: {char_on2: True, char_brightness2: 12}}
    )


def test_accessory_level_callbacks_with_a_failure(driver: AccessoryDriver) -> None:
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    switch_service = Service(uuid1(), "Switch")
    char_switch_on = Characteristic("On", uuid1(), CHAR_PROPS)
    switch_service.add_characteristic(char_switch_on)

    mock_callback = MagicMock()
    acc.setter_callback = mock_callback

    acc.add_service(service)
    acc.add_service(switch_service)
    bridge.add_accessory(acc)

    service2 = Service(uuid1(), "Lightbulb")
    char_on2 = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness2 = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service2.add_characteristic(char_on2)
    service2.add_characteristic(char_brightness2)

    mock_callback2 = MagicMock(side_effect=OSError)
    acc2.setter_callback = mock_callback2

    acc2.add_service(service2)
    bridge.add_accessory(acc2)

    char_switch_on_iid = char_switch_on.to_HAP()[HAP_REPR_IID]
    char_on_iid = char_on.to_HAP()[HAP_REPR_IID]
    char_brightness_iid = char_brightness.to_HAP()[HAP_REPR_IID]
    char_on2_iid = char_on2.to_HAP()[HAP_REPR_IID]
    char_brightness2_iid = char_brightness2.to_HAP()[HAP_REPR_IID]

    driver.add_accessory(bridge)

    response = driver.set_characteristics(
        {
            HAP_REPR_CHARS: [
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_switch_on_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc.aid,
                    HAP_REPR_IID: char_brightness_iid,
                    HAP_REPR_VALUE: 88,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_on2_iid,
                    HAP_REPR_VALUE: True,
                },
                {
                    HAP_REPR_AID: acc2.aid,
                    HAP_REPR_IID: char_brightness2_iid,
                    HAP_REPR_VALUE: 12,
                },
            ]
        },
        "mock_addr",  # ty: ignore[invalid-argument-type]
    )

    mock_callback.assert_called_with(
        {
            service: {char_on: True, char_brightness: 88},
            switch_service: {char_switch_on: True},
        }
    )
    mock_callback2.assert_called_with(
        {service2: {char_on2: True, char_brightness2: 12}}
    )

    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_on_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_switch_on_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_brightness_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SUCCESS,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_on2_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_brightness2_iid,
                HAP_REPR_STATUS: HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE,
            },
        ]
    }


async def test_start_stop_sync_acc(async_zeroconf: Any) -> None:
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        run_event = asyncio.Event()

        class Acc(Accessory):
            @Accessory.run_at_interval(0)
            def run(self) -> None:
                run_event.set()

            def setup_message(self) -> None:
                pass

        acc = Acc(driver, "TestAcc")
        driver.add_accessory(acc)
        driver.start_service()
        await run_event.wait()
        assert driver.state.config_version == 2
        assert not driver.loop.is_closed()
        await driver.async_stop()
        assert not driver.loop.is_closed()


async def test_start_stop_async_acc(async_zeroconf: Any) -> None:
    """Verify run_at_interval closes the driver."""
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(
            loop=asyncio.get_event_loop(), interface_choice=InterfaceChoice.Default
        )
        run_event = asyncio.Event()

        class Acc(Accessory):
            @Accessory.run_at_interval(0)
            async def run(self) -> None:
                run_event.set()

            def setup_message(self) -> None:
                pass

        acc = Acc(driver, "TestAcc")
        driver.add_accessory(acc)
        driver.start_service()
        await asyncio.sleep(0)
        await run_event.wait()
        assert driver.state.config_version == 2
        assert not driver.loop.is_closed()
        await driver.async_stop()
        assert not driver.loop.is_closed()

        run_event.clear()
        driver.start_service()
        await asyncio.sleep(0)
        await run_event.wait()
        assert driver.state.config_version == 2
        await driver.async_stop()
        assert not driver.loop.is_closed()
        acc.add_preload_service("GarageDoorOpener")

        # Adding a new service should increment the config version
        run_event.clear()
        driver.start_service()
        await asyncio.sleep(0)
        await run_event.wait()
        assert driver.state.config_version == 3
        await driver.async_stop()
        assert not driver.loop.is_closed()

        # But only once
        run_event.clear()
        driver.start_service()
        await asyncio.sleep(0)
        await run_event.wait()
        assert driver.state.config_version == 3
        await driver.async_stop()
        assert not driver.loop.is_closed()


async def test_start_from_async_stop_from_executor(async_zeroconf: Any) -> None:
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        run_event = asyncio.Event()

        class Acc(Accessory):
            @Accessory.run_at_interval(0)
            def run(self) -> None:
                run_event.set()

            def setup_message(self) -> None:
                pass

        acc = Acc(driver, "TestAcc")
        driver.add_accessory(acc)
        driver.start_service()
        await run_event.wait()
        assert not driver.loop.is_closed()
        await driver.loop.run_in_executor(None, driver.stop)
        assert driver.aio_stop_event is not None
        await driver.aio_stop_event.wait()


def test_start_without_accessory(driver: AccessoryDriver) -> None:
    """Verify we throw ValueError if there is no accessory."""
    with pytest.raises(ValueError):
        driver.start_service()


def test_send_events(driver: AccessoryDriver) -> None:
    """Test we can send events."""
    driver.aio_stop_event = MagicMock(is_set=MagicMock(return_value=False))

    class LoopMock:
        runcount = 0

        def is_closed(self) -> bool:
            self.runcount += 1
            if self.runcount > 1:
                return True
            return False

    class HapServerMock:
        pushed_events = set()

        def push_event(
            self, bytedata: bytes, client_addr: Any, immediate: bool
        ) -> bool:
            self.pushed_events.add((bytedata, client_addr))
            if client_addr == "client2":
                return False
            return True

        def get_pushed_events(self) -> set[Any]:
            return self.pushed_events

    # Inject a fake server to capture pushed events; it is
    # intentionally not a HAPServer subclass.
    driver.http_server = HapServerMock()  # ty: ignore[invalid-assignment]
    driver.loop = LoopMock()
    driver.topics = {"mocktopic": {"client1", "client2", "client3"}}
    driver.async_send_event("mocktopic", "bytedata", "client1", True)  # ty: ignore[invalid-argument-type]

    # Only client2 and client3 get the event when client1 sent it
    # Read back through the injected fake above, not the typed attribute.
    pushed = driver.http_server.get_pushed_events()  # ty: ignore[missing-attribute]
    assert pushed == {
        ("bytedata", "client2"),
        ("bytedata", "client3"),
    }


def test_async_subscribe_client_topic(driver: AccessoryDriver) -> None:
    """Test subscribe and unsubscribe."""
    addr_info = ("1.2.3.4", 5)
    topic = "any"
    assert driver.topics == {}
    driver.async_subscribe_client_topic(addr_info, topic, True)
    assert driver.topics == {topic: {addr_info}}
    driver.async_subscribe_client_topic(addr_info, topic, False)
    assert driver.topics == {}
    driver.async_subscribe_client_topic(addr_info, "invalid", False)
    assert driver.topics == {}


def test_mdns_service_info(driver: AccessoryDriver) -> None:
    """Test accessory mdns advert."""
    acc = Accessory(driver, "[@@@Test@@@] Accessory")
    driver.add_accessory(acc)
    addr = "172.0.0.1"
    mac = "00:00:00:00:00:00"
    pin = b"123-45-678"
    port = 11111
    state = State(address=addr, mac=mac, pincode=pin, port=port)
    state.setup_id = "abc"
    mdns_info = AccessoryMDNSServiceInfo(acc, state)
    assert mdns_info.type == "_hap._tcp.local."
    assert mdns_info.name == "Test Accessory 000000._hap._tcp.local."
    assert mdns_info.server == "Test-Accessory-000000.local."
    assert mdns_info.port == port
    assert mdns_info.addresses == [b"\xac\x00\x00\x01"]
    assert mdns_info.decoded_properties == {
        "md": "Test Accessory",
        "pv": "1.1",
        "id": "00:00:00:00:00:00",
        "c#": "1",
        "s#": "1",
        "ff": "0",
        "ci": "1",
        "sf": "1",
        "sh": "+KjpzQ==",
    }


def test_mdns_service_info_with_specified_server(driver: AccessoryDriver) -> None:
    """Test accessory mdns advert when the server is specified."""
    acc = Accessory(driver, "Test Accessory")
    driver.add_accessory(acc)
    addr = "172.0.0.1"
    mac = "00:00:00:00:00:00"
    pin = b"123-45-678"
    port = 11111
    state = State(address=addr, mac=mac, pincode=pin, port=port)
    state.setup_id = "abc"
    mdns_info = AccessoryMDNSServiceInfo(acc, state, "hap1.local.")
    assert mdns_info.type == "_hap._tcp.local."
    assert mdns_info.name == "Test Accessory 000000._hap._tcp.local."
    assert mdns_info.server == "hap1.local."
    assert mdns_info.port == port
    assert mdns_info.addresses == [b"\xac\x00\x00\x01"]
    assert mdns_info.decoded_properties == {
        "md": "Test Accessory",
        "pv": "1.1",
        "id": "00:00:00:00:00:00",
        "c#": "1",
        "s#": "1",
        "ff": "0",
        "ci": "1",
        "sf": "1",
        "sh": "+KjpzQ==",
    }


@pytest.mark.parametrize(
    "accessory_name, mdns_name, mdns_server",
    [
        (
            "--h a p p y--",
            "h a p p y AbcDEF._hap._tcp.local.",
            "h-a-p-p-y-AbcDEF.local.",
        ),
        (
            "--H A P P Y--",
            "H A P P Y AbcDEF._hap._tcp.local.",
            "H-A-P-P-Y-AbcDEF.local.",
        ),
        (
            "- - H---A---P---P---Y - -",
            "H---A---P---P---Y AbcDEF._hap._tcp.local.",
            "H-A-P-P-Y-AbcDEF.local.",
        ),
    ],
)
def test_mdns_name_sanity(
    driver: AccessoryDriver,
    accessory_name: str,
    mdns_name: str,
    mdns_server: str,
) -> None:
    """Test mdns name sanity."""
    acc = Accessory(driver, accessory_name)
    driver.add_accessory(acc)
    addr = "172.0.0.1"
    mac = "00:00:00:Ab:cD:EF"
    pin = b"123-45-678"
    port = 11111
    state = State(address=addr, mac=mac, pincode=pin, port=port)
    state.setup_id = "abc"
    mdns_info = AccessoryMDNSServiceInfo(acc, state)
    assert mdns_info.type == "_hap._tcp.local."
    assert mdns_info.name == mdns_name
    assert mdns_info.server == mdns_server


async def test_start_service_and_update_config(async_zeroconf: Any) -> None:
    """Test starting service and updating the config."""
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        acc = Accessory(driver, "TestAcc")
        driver.add_accessory(acc)
        await driver.async_start()

        assert driver.state.config_version == 2
        driver.config_changed()
        assert driver.state.config_version == 3
        driver.state.config_version = 65535
        driver.config_changed()
        assert driver.state.config_version == 1
        for _ in range(3):
            await asyncio.sleep(0)
        await driver.async_stop()
        await asyncio.sleep(0)
        assert not driver.loop.is_closed()
        assert driver.aio_stop_event.is_set()


async def test_start_without_advertising() -> None:
    """Test starting and stopping with mDNS advertising disabled.

    No zeroconf instance should be created and none of the
    advertisement methods should be called.
    """
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
        patch("pyhap.accessory_driver.AsyncZeroconf") as mock_async_zeroconf,
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop(), advertise=False)
        acc = Accessory(driver, "TestAcc")
        driver.add_accessory(acc)
        await driver.async_start()

        assert driver.advertiser is None
        assert driver.mdns_service_info is None
        assert not mock_async_zeroconf.called

        # Pairing changes must not crash when advertising is disabled.
        driver.async_update_advertisement()

        for _ in range(3):
            await asyncio.sleep(0)
        await driver.async_stop()
        await asyncio.sleep(0)
        assert not driver.loop.is_closed()
        assert driver.aio_stop_event is not None
        assert driver.aio_stop_event.is_set()


def test_call_add_job_with_none(driver: AccessoryDriver) -> None:
    """Test calling add job with none."""
    with pytest.raises(ValueError):
        driver.add_job(None)


async def test_call_async_add_job_with_coroutine(driver: AccessoryDriver) -> None:
    """Test calling async_add_job with a coroutine."""
    with (
        patch("pyhap.accessory_driver.HAPServer"),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        called = False

        async def coro_test() -> None:
            nonlocal called
            called = True

        await driver.async_add_job(coro_test)
        assert called is True

        called = False
        await driver.async_add_job(coro_test())
        assert called is True


async def test_call_async_add_job_with_callback(
    driver: AccessoryDriver, async_zeroconf: Any
) -> None:
    """Test calling async_add_job with a coroutine."""
    with (
        patch("pyhap.accessory_driver.HAPServer"),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        called = False

        @util.callback
        def callback_test() -> None:
            nonlocal called
            called = True

        driver.async_add_job(callback_test)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert called is True


async def test_bridge_with_multiple_async_run_at_interval_accessories(
    async_zeroconf: Any,
) -> None:
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        bridge = Bridge(driver, "mybridge")
        acc = AsyncIntervalAccessory(driver, "TestAcc", aid=2)
        acc2 = AsyncIntervalAccessory(driver, "TestAcc2", aid=3)
        acc3 = AsyncIntervalAccessory(driver, "TestAcc3", aid=4)
        bridge.add_accessory(acc)
        bridge.add_accessory(acc2)
        bridge.add_accessory(acc3)
        driver.add_accessory(bridge)
        driver.start_service()
        await asyncio.sleep(0.5)
        assert not driver.loop.is_closed()
        await driver.async_stop()

    assert acc.counter > 2
    assert acc2.counter > 2
    assert acc3.counter > 2


async def test_bridge_with_multiple_sync_run_at_interval_accessories(
    async_zeroconf: Any,
) -> None:
    with (
        patch("pyhap.accessory_driver.HAPServer.async_stop", new_callable=MagicMock),
        patch("pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock),
        patch("pyhap.accessory_driver.AccessoryDriver.persist"),
        patch("pyhap.accessory_driver.AccessoryDriver.load"),
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        bridge = Bridge(driver, "mybridge")
        acc = SyncIntervalAccessory(driver, "TestAcc", aid=2)
        acc2 = SyncIntervalAccessory(driver, "TestAcc2", aid=3)
        acc3 = SyncIntervalAccessory(driver, "TestAcc3", aid=4)
        bridge.add_accessory(acc)
        bridge.add_accessory(acc2)
        bridge.add_accessory(acc3)
        driver.add_accessory(bridge)
        driver.start_service()
        await asyncio.sleep(0.5)
        assert not driver.loop.is_closed()
        await driver.async_stop()

    assert acc.counter > 2
    assert acc2.counter > 2
    assert acc3.counter > 2


def test_hash_ignores_values(driver: AccessoryDriver) -> None:
    """The hash should change when the config changes but not for a value change."""
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    switch_service = Service(uuid1(), "Switch")
    char_switch_on = Characteristic("On", uuid1(), CHAR_PROPS)
    switch_service.add_characteristic(char_switch_on)

    mock_callback = MagicMock()
    acc.setter_callback = mock_callback

    acc.add_service(service)
    acc.add_service(switch_service)
    bridge.add_accessory(acc)

    service2 = Service(uuid1(), "Lightbulb")
    char_on2 = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness2 = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service2.add_characteristic(char_on2)
    service2.add_characteristic(char_brightness2)

    mock_callback2 = MagicMock(side_effect=OSError)
    acc2.setter_callback = mock_callback2

    bridge.add_accessory(acc2)

    driver.add_accessory(bridge)

    original_hash = driver.accessories_hash

    char_on.set_value(False)
    char_on2.set_value(False)
    char_brightness.set_value(88)
    char_switch_on.set_value(True)

    assert driver.accessories_hash == original_hash

    acc2.add_service(service2)
    new_hash = driver.accessories_hash
    assert new_hash != original_hash

    char_brightness2.set_value(43)

    assert driver.accessories_hash == new_hash


def _hap_connection(
    driver: AccessoryDriver, client: Any, shared_key: bytes | None = None
) -> HAPServerProtocol:
    """Register a HAP connection from ``client``, encrypted with ``shared_key``."""
    transport = MagicMock()
    transport.get_extra_info.return_value = client
    connection = HAPServerProtocol(MagicMock(), driver.http_server.connections, driver)
    connection.connection_made(transport)
    # As set by pair-verify.
    connection._shared_key = shared_key
    return connection


@pytest.mark.parametrize(
    "client", [("10.0.0.9", 5000), ("fe80::1", 5000, 0, 2)], ids=["ipv4", "ipv6"]
)
def test_derive_session_key(driver: AccessoryDriver, client: Any) -> None:
    """Test keys are derived only from the session key of an encrypted connection."""
    driver.add_accessory(Accessory(driver, "TestAcc"))
    salt, info = b"salt", b"info"
    assert driver.derive_session_key(client, salt, info) is None

    connection = _hap_connection(driver, client)
    assert driver.derive_session_key(client, salt, info) is None

    shared_key = os.urandom(32)
    connection._shared_key = shared_key
    key = driver.derive_session_key(client, salt, info)
    assert key == hap_hkdf(shared_key, salt, info)
    assert key != shared_key
    assert driver.derive_session_key(("10.0.0.9", 5001), salt, info) is None

    connection.close()
    assert driver.derive_session_key(client, salt, info) is None


def test_derive_session_key_without_client_addr(driver: AccessoryDriver) -> None:
    """Test a write without a sender address never matches a connection."""
    driver.add_accessory(Accessory(driver, "TestAcc"))
    _hap_connection(driver, None, os.urandom(32))
    assert driver.derive_session_key(None, b"salt", b"info") is None


def test_pairing_rate_limit_lockout_and_reset(driver: AccessoryDriver) -> None:
    """Verify the pair-setup lockout engages, expires and resets on success."""
    with patch("pyhap.accessory_driver.time.monotonic", return_value=100.0) as mock_now:
        assert not driver.pairing_locked_out

        # One below the threshold: no lockout yet.
        for _ in range(MAX_FAILED_PAIRING_ATTEMPTS - 1):
            driver.register_failed_pairing_attempt()
        assert not driver.pairing_locked_out

        # The threshold failure engages the lockout.
        driver.register_failed_pairing_attempt()
        assert driver.pairing_locked_out

        # The lockout expires after the cooldown.
        mock_now.return_value = 100.0 + PAIRING_LOCKOUT_SECONDS
        assert not driver.pairing_locked_out

        # A successful pairing resets the counter mid-streak.
        for _ in range(MAX_FAILED_PAIRING_ATTEMPTS - 1):
            driver.register_failed_pairing_attempt()
        driver.pair(str(uuid1()).encode(), b"public-key", HAP_PERMISSIONS.ADMIN)
        assert driver._failed_pairing_attempts == 0
        assert driver._pairing_lockout_until == 0.0
        driver.register_failed_pairing_attempt()
        assert not driver.pairing_locked_out


def test_prepare_bounds_prepared_writes(driver: AccessoryDriver) -> None:
    """Verify the TTL is capped and pending writes per client are bounded."""
    client_addr = ("1.2.3.4", 5)

    # An absurd TTL is clamped to the maximum.
    driver.prepare({HAP_REPR_TTL: 10_000_000_000, HAP_REPR_PID: 1}, client_addr)
    assert (
        driver.prepared_writes[client_addr][1] - time.time()
        <= MAX_PREPARED_WRITE_TTL_SECONDS
    )

    # Registering more unique pids than the cap evicts the soonest-to-expire
    # entry instead of growing the dict without bound.
    for pid in range(MAX_PREPARED_WRITES_PER_CLIENT + 5):
        driver.prepare({HAP_REPR_TTL: 10_000, HAP_REPR_PID: pid}, client_addr)

    pending = driver.prepared_writes[client_addr]
    assert len(pending) == MAX_PREPARED_WRITES_PER_CLIENT
    # The earliest-registered pids were evicted, the latest kept.
    assert 0 not in pending
    assert MAX_PREPARED_WRITES_PER_CLIENT + 4 in pending
