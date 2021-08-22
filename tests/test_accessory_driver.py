"""Tests for pyhap.accessory_driver."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import tempfile
from unittest.mock import MagicMock, patch
from uuid import uuid1

from cryptography.hazmat.primitives import serialization
import pytest
from zeroconf import InterfaceChoice

from pyhap import util
from pyhap.accessory import STANDALONE_AID, Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver, AccessoryMDNSServiceInfo
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
    HAP_SERVER_STATUS,
)
from pyhap.service import Service
from pyhap.state import State

from . import AsyncMock

CHAR_PROPS = {
    PROP_FORMAT: HAP_FORMAT_INT,
    PROP_PERMISSIONS: HAP_PERMISSION_READ,
}


class AsyncIntervalAccessory(Accessory):
    """An accessory increments a counter at interval."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._counter = 0

    @Accessory.run_at_interval(0.001)  # Run this method every 0.001 seconds
    async def run(self):
        self._counter += 1

    @property
    def counter(self):
        return self._counter


class SyncIntervalAccessory(Accessory):
    """An accessory increments a counter at interval."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._counter = 0

    @Accessory.run_at_interval(0.001)  # Run this method every 0.001 seconds
    def run(self):  # pylint: disable=invalid-overridden-method
        self._counter += 1

    @property
    def counter(self):
        return self._counter


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


def test_persist_load(async_zeroconf):
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


def test_persist_cannot_write(async_zeroconf):
    with tempfile.NamedTemporaryFile(mode="r+") as file:
        with patch("pyhap.accessory_driver.HAPServer"):
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.persist_file = "/file/that/will/not/exist"
            with pytest.raises(OSError):
                driver.persist()


def test_external_zeroconf():
    zeroconf = MagicMock()
    with patch("pyhap.accessory_driver.HAPServer"), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ):
        driver = AccessoryDriver(port=51234, async_zeroconf_instance=zeroconf)
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


def test_accessory_level_callbacks(driver):
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
        "mock_addr",
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


def test_accessory_level_callbacks_with_a_failure(driver):
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
        "mock_addr",
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


@pytest.mark.asyncio
async def test_start_stop_sync_acc(async_zeroconf):
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
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
        assert driver.state.config_version == 2
        assert not driver.loop.is_closed()
        await driver.async_stop()
        assert not driver.loop.is_closed()


@pytest.mark.asyncio
async def test_start_stop_async_acc(async_zeroconf):
    """Verify run_at_interval closes the driver."""
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
    ):
        driver = AccessoryDriver(
            loop=asyncio.get_event_loop(), interface_choice=InterfaceChoice.Default
        )
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


@pytest.mark.asyncio
async def test_start_from_async_stop_from_executor(async_zeroconf):
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
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
        await driver.loop.run_in_executor(None, driver.stop)
        await driver.aio_stop_event.wait()


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

        def push_event(self, bytedata, client_addr, immediate):
            self.pushed_events.add((bytedata, client_addr))
            if client_addr == "client2":
                return False
            return True

        def get_pushed_events(self):
            return self.pushed_events

    driver.http_server = HapServerMock()
    driver.loop = LoopMock()
    driver.topics = {"mocktopic": {"client1", "client2", "client3"}}
    driver.async_send_event("mocktopic", "bytedata", "client1", True)

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
    assert mdns_info.server == "Test-Accessory-000000.local."
    assert mdns_info.port == port
    assert mdns_info.addresses == [b"\xac\x00\x00\x01"]
    assert mdns_info.properties == {
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
def test_mdns_name_sanity(driver, accessory_name, mdns_name, mdns_server):
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


@pytest.mark.asyncio
async def test_start_service_and_update_config(async_zeroconf):
    """Test starting service and updating the config."""
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
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


def test_call_add_job_with_none(driver):
    """Test calling add job with none."""
    with pytest.raises(ValueError):
        driver.add_job(None)


@pytest.mark.asyncio
async def test_call_async_add_job_with_coroutine(driver):
    """Test calling async_add_job with a coroutine."""
    with patch("pyhap.accessory_driver.HAPServer"), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch("pyhap.accessory_driver.AccessoryDriver.load"):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        called = False

        async def coro_test():
            nonlocal called
            called = True

        await driver.async_add_job(coro_test)
        assert called is True

        called = False
        await driver.async_add_job(coro_test())
        assert called is True


@pytest.mark.asyncio
async def test_call_async_add_job_with_callback(driver, async_zeroconf):
    """Test calling async_add_job with a coroutine."""
    with patch("pyhap.accessory_driver.HAPServer"), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch("pyhap.accessory_driver.AccessoryDriver.load"):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        called = False

        @util.callback
        def callback_test():
            nonlocal called
            called = True

        driver.async_add_job(callback_test)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert called is True


@pytest.mark.asyncio
async def test_bridge_with_multiple_async_run_at_interval_accessories(async_zeroconf):
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
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


@pytest.mark.asyncio
async def test_bridge_with_multiple_sync_run_at_interval_accessories(async_zeroconf):
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
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
