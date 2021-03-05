"""Tests for pyhap.accessory_driver."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import tempfile
from unittest.mock import MagicMock, patch
from uuid import uuid1

import pytest

from pyhap.accessory import STANDALONE_AID, Accessory, Bridge
from pyhap.accessory_driver import (
    SERVICE_COMMUNICATION_FAILURE,
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
    HAP_REPR_AID,
    HAP_REPR_CHARS,
    HAP_REPR_IID,
    HAP_REPR_STATUS,
    HAP_REPR_VALUE,
)
from pyhap.service import Service
from pyhap.state import State

from . import AsyncMock

CHAR_PROPS = {
    PROP_FORMAT: HAP_FORMAT_INT,
    PROP_PERMISSIONS: HAP_PERMISSION_READ,
}


class UnavailableAccessory(Accessory):
    """An accessory that is not available."""

    @property
    def available(self):
        return False


def test_auto_add_aid_mac(driver):
    acc = Accessory(driver, "Test Accessory")
    driver.add_accessory(acc)
    assert acc.aid == STANDALONE_AID
    assert driver.state.mac is not None


def test_not_standalone_aid(driver):
    acc = Accessory(driver, "Test Accessory", aid=STANDALONE_AID + 1)
    with pytest.raises(ValueError):
        driver.add_accessory(acc)


def test_persist_load():
    with tempfile.NamedTemporaryFile(mode="r+") as file:
        with patch("pyhap.accessory_driver.HAPServer"), patch(
            "pyhap.accessory_driver.Zeroconf"
        ):
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.persist()
            pk = driver.state.public_key
            # Re-start driver with a "new" accessory. State gets loaded into
            # the new accessory.
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.load()
    assert driver.state.public_key == pk


def test_persist_cannot_write():
    with tempfile.NamedTemporaryFile(mode="r+") as file:
        with patch("pyhap.accessory_driver.HAPServer"), patch(
            "pyhap.accessory_driver.Zeroconf"
        ):
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.persist_file = "/file/that/will/not/exist"
            with pytest.raises(OSError):
                driver.persist()


def test_external_zeroconf():
    zeroconf = MagicMock()
    with patch("pyhap.accessory_driver.HAPServer"), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ):
        driver = AccessoryDriver(port=51234, zeroconf_instance=zeroconf)
    assert driver.advertiser == zeroconf


def test_service_callbacks(driver):
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
        "mock_addr",
    )
    assert response is None

    mock_callback2.assert_called_with({"On": True, "Brightness": 12})
    mock_callback.assert_called_with({"On": True, "Brightness": 88})

    get_chars = driver.get_characteristics(
        ["{}.{}".format(acc.aid, char_on_iid), "{}.{}".format(acc2.aid, char_on2_iid)]
    )
    assert get_chars == {
        "characteristics": [
            {"aid": acc.aid, "iid": char_on_iid, "status": 0, "value": True},
            {"aid": acc2.aid, "iid": char_on2_iid, "status": -70402},
        ]
    }

    def _fail_func():
        raise ValueError

    char_brightness.getter_callback = _fail_func
    get_chars = driver.get_characteristics(
        [
            "{}.{}".format(acc.aid, char_on_iid),
            "{}.{}".format(acc2.aid, char_on2_iid),
            "{}.{}".format(acc2.aid, char_brightness_iid),
            "{}.{}".format(acc.aid, char_brightness2_iid),
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


def test_service_callbacks_partial_failure(driver):
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    def fail_callback(*_):
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
        "mock_addr",
    )

    mock_callback2.assert_called_with({"On": True, "Brightness": 12})
    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_on_iid,
                HAP_REPR_STATUS: SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_brightness_iid,
                HAP_REPR_STATUS: SERVICE_COMMUNICATION_FAILURE,
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


def test_mixing_service_char_callbacks_partial_failure(driver):
    bridge = Bridge(driver, "mybridge")
    acc = Accessory(driver, "TestAcc", aid=2)
    acc2 = UnavailableAccessory(driver, "TestAcc2", aid=3)

    service = Service(uuid1(), "Lightbulb")
    char_on = Characteristic("On", uuid1(), CHAR_PROPS)
    char_brightness = Characteristic("Brightness", uuid1(), CHAR_PROPS)

    service.add_characteristic(char_on)
    service.add_characteristic(char_brightness)

    def fail_callback(*_):
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
        "mock_addr",
    )

    assert response == {
        HAP_REPR_CHARS: [
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_on_iid,
                HAP_REPR_STATUS: SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc.aid,
                HAP_REPR_IID: char_brightness_iid,
                HAP_REPR_STATUS: SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_on2_iid,
                HAP_REPR_STATUS: SERVICE_COMMUNICATION_FAILURE,
            },
            {
                HAP_REPR_AID: acc2.aid,
                HAP_REPR_IID: char_brightness2_iid,
                HAP_REPR_STATUS: 0,
            },
        ]
    }


def test_start_from_sync(driver):
    """Start from sync."""

    class Acc(Accessory):
        @Accessory.run_at_interval(0)
        async def run(self):
            driver.executor = ThreadPoolExecutor()
            driver.loop.set_default_executor(driver.executor)
            await driver.async_stop()

        def setup_message(self):
            pass

    acc = Acc(driver, "TestAcc")
    driver.add_accessory(acc)
    driver.start()


@pytest.mark.asyncio
async def test_start_stop_sync_acc():
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.Zeroconf"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        run_event = asyncio.Event()

        class Acc(Accessory):
            @Accessory.run_at_interval(0)
            def run(self):  # pylint: disable=invalid-overridden-method
                run_event.set()

            def setup_message(self):
                pass

        acc = Acc(driver, "TestAcc")
        driver.add_accessory(acc)
        driver.start_service()
        await run_event.wait()
        assert not driver.loop.is_closed()
        await driver.async_stop()
        assert not driver.loop.is_closed()


@pytest.mark.asyncio
async def test_start_stop_async_acc():
    """Verify run_at_interval closes the driver."""
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.Zeroconf"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        run_event = asyncio.Event()

        class Acc(Accessory):
            @Accessory.run_at_interval(0)
            async def run(self):
                run_event.set()

            def setup_message(self):
                pass

        acc = Acc(driver, "TestAcc")
        driver.add_accessory(acc)
        driver.start_service()
        await asyncio.sleep(0)
        await run_event.wait()
        assert not driver.loop.is_closed()
        await driver.async_stop()
        assert not driver.loop.is_closed()


def test_start_without_accessory(driver):
    """Verify we throw ValueError if there is no accessory."""
    with pytest.raises(ValueError):
        driver.start_service()


def test_send_events(driver):
    """Test we can send events."""
    driver.aio_stop_event = MagicMock(is_set=MagicMock(return_value=False))

    class LoopMock:
        runcount = 0

        def is_closed(self):
            self.runcount += 1
            if self.runcount > 1:
                return True
            return False

    class HapServerMock:
        pushed_events = set()

        def push_event(self, bytedata, client_addr):
            self.pushed_events.add((bytedata, client_addr))
            if client_addr == "client2":
                return False
            return True

        def get_pushed_events(self):
            return self.pushed_events

    driver.http_server = HapServerMock()
    driver.loop = LoopMock()
    driver.topics = {"mocktopic": {"client1", "client2", "client3"}}
    driver.async_send_event("mocktopic", "bytedata", "client1")

    # Only client2 and client3 get the event when client1 sent it
    assert driver.http_server.get_pushed_events() == {
        ("bytedata", "client2"),
        ("bytedata", "client3"),
    }


def test_async_subscribe_client_topic(driver):
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


def test_mdns_service_info(driver):
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
    assert mdns_info.port == port
    assert mdns_info.addresses == [b"\xac\x00\x00\x01"]
    assert mdns_info.properties == {
        "md": "Test Accessory",
        "pv": "1.0",
        "id": "00:00:00:00:00:00",
        "c#": "2",
        "s#": "1",
        "ff": "0",
        "ci": "1",
        "sf": "1",
        "sh": "+KjpzQ==",
    }


@pytest.mark.asyncio
async def test_start_service_and_update_config():
    """Test starting service and updating the config."""
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.Zeroconf"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
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
