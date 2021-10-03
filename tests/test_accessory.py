"""Tests for pyhap.accessory."""
import asyncio
from io import StringIO
from unittest.mock import patch

import pytest

from pyhap import accessory
from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (
    CATEGORY_CAMERA,
    CATEGORY_TARGET_CONTROLLER,
    CATEGORY_TELEVISION,
    HAP_REPR_VALUE,
    STANDALONE_AID,
)
from pyhap.service import Service
from pyhap.state import State

from . import AsyncMock

# #### Accessory ######
# execute with `-k acc`
# #####################


class TestAccessory(Accessory):
    """An accessory that keeps track of if its stopped."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stopped = False

    async def stop(self):
        self._stopped = True

    @property
    def stopped(self):
        return self._stopped


def test_acc_init(mock_driver):
    Accessory(mock_driver, "Test Accessory")


def test_acc_publish_no_broker(mock_driver):
    acc = Accessory(mock_driver, "Test Accessory")
    service = acc.driver.loader.get_service("TemperatureSensor")
    char = service.get_characteristic("CurrentTemperature")
    acc.add_service(service)
    char.set_value(25, should_notify=True)


def test_acc_set_primary_service(mock_driver):
    """Test method set_primary_service."""
    acc = Accessory(mock_driver, "Test Accessory")
    service = acc.driver.loader.get_service("Television")
    acc.add_service(service)
    assert "Television" in str(acc)
    linked_service = acc.driver.loader.get_service("TelevisionSpeaker")
    acc.add_service(linked_service)
    assert acc.get_service("Television").is_primary_service is None
    assert acc.get_service("TelevisionSpeaker").is_primary_service is None
    acc.set_primary_service(service)
    assert acc.get_service("Television").is_primary_service is True
    assert acc.get_service("TelevisionSpeaker").is_primary_service is False


def test_acc_add_preload_service_without_chars(mock_driver):
    """Test method add_preload_service."""
    acc = Accessory(mock_driver, "Test Accessory")

    serv = acc.add_preload_service("Television")
    assert isinstance(serv, Service)


def test_acc_add_preload_service_with_chars(mock_driver):
    """Test method add_preload_service with additional chars."""
    acc = Accessory(mock_driver, "Test Accessory")

    serv = acc.add_preload_service("Television", chars=["ActiveIdentifier"])
    assert isinstance(serv, Service)
    assert serv.get_characteristic("ActiveIdentifier") is not None


# #### Bridge ############
# execute with `-k bridge`
# ########################


def test_bridge_init(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    assert bridge.available is True


def test_bridge_add_accessory(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    acc = Accessory(mock_driver, "Test Accessory", aid=2)
    assert acc.available is True
    bridge.add_accessory(acc)
    acc2 = Accessory(mock_driver, "Test Accessory 2")
    bridge.add_accessory(acc2)
    assert acc2.aid not in (STANDALONE_AID, acc.aid)


def test_bridge_n_add_accessory_bridge_aid(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    acc = Accessory(mock_driver, "Test Accessory", aid=STANDALONE_AID)
    with pytest.raises(ValueError):
        bridge.add_accessory(acc)


def test_bridge_n_add_accessory_dup_aid(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    acc_1 = Accessory(mock_driver, "Test Accessory 1", aid=2)
    acc_2 = Accessory(mock_driver, "Test Accessory 2", aid=acc_1.aid)
    bridge.add_accessory(acc_1)
    with pytest.raises(ValueError):
        bridge.add_accessory(acc_2)


@patch("sys.stdout", new_callable=StringIO)
def test_setup_message_without_qr_code(mock_stdout, mock_driver):
    """Verify we print out the setup code."""
    acc = Accessory(mock_driver, "Test Accessory", aid=STANDALONE_AID)
    mock_driver.state = State(
        address="1.2.3.4", mac="AA::BB::CC::DD::EE", pincode=b"653-32-1211", port=44
    )
    with patch.object(accessory, "SUPPORT_QR_CODE", False):
        acc.setup_message()
    assert "653-32-1211" in mock_stdout.getvalue()


@patch("sys.stdout", new_callable=StringIO)
def test_setup_message_with_qr_code(mock_stdout, mock_driver):
    """Verify we can print out a QR code."""
    acc = Accessory(mock_driver, "Test Accessory", aid=STANDALONE_AID)
    mock_driver.state = State(
        address="1.2.3.4", mac="AA::BB::CC::DD::EE", pincode=b"653-32-1211", port=44
    )
    with patch.object(accessory, "SUPPORT_QR_CODE", True):
        acc.setup_message()
    assert "653-32-1211" in mock_stdout.getvalue()
    assert "\x1b[7m" in mock_stdout.getvalue()


def test_xhm_uri(mock_driver):
    acc_1 = Accessory(mock_driver, "Test Accessory 1", aid=2)
    acc_1.category = CATEGORY_CAMERA
    mock_driver.state = State(
        address="1.2.3.4", mac="AA::BB::CC::DD::EE", pincode=b"653-32-1211", port=44
    )
    mock_driver.state.setup_id = "AAAA"
    assert acc_1.xhm_uri() == "X-HM://00H708WSBAAAA"

    acc_1.category = CATEGORY_TELEVISION
    mock_driver.state = State(
        address="1.2.3.4", mac="AA::BB::CC::DD::EE", pincode=b"323-23-1212", port=44
    )
    mock_driver.state.setup_id = "BBBB"
    assert acc_1.xhm_uri() == "X-HM://00UQBOTF0BBBB"

    acc_1.category = CATEGORY_TARGET_CONTROLLER
    mock_driver.state = State(
        address="1.2.3.4", mac="AA::BB::CC::DD::EE", pincode=b"323-23-1212", port=44
    )
    mock_driver.state.setup_id = "BBBB"
    assert acc_1.xhm_uri() == "X-HM://00VPU8UEKBBBB"


def test_set_info_service(mock_driver):
    acc_1 = Accessory(mock_driver, "Test Accessory 1", aid=2)
    acc_1.set_info_service("firmware", "manufacturer", "model", "serial")
    serv_info = acc_1.get_service("AccessoryInformation")
    assert serv_info.get_characteristic("FirmwareRevision").value == "firmware"
    assert serv_info.get_characteristic("Manufacturer").value == "manufacturer"
    assert serv_info.get_characteristic("Model").value == "model"
    assert serv_info.get_characteristic("SerialNumber").value == "serial"


def test_set_info_service_empty(mock_driver):
    acc_1 = Accessory(mock_driver, "Test Accessory 1", aid=2)
    acc_1.set_info_service()
    serv_info = acc_1.get_service("AccessoryInformation")
    assert serv_info.get_characteristic("FirmwareRevision").value == ""
    assert serv_info.get_characteristic("Manufacturer").value == ""
    assert serv_info.get_characteristic("Model").value == ""
    assert serv_info.get_characteristic("SerialNumber").value == "default"


def test_set_info_service_invalid_serial(mock_driver):
    acc_1 = Accessory(mock_driver, "Test Accessory 1", aid=2)
    acc_1.set_info_service(serial_number="")
    serv_info = acc_1.get_service("AccessoryInformation")
    assert serv_info.get_characteristic("FirmwareRevision").value == ""
    assert serv_info.get_characteristic("Manufacturer").value == ""
    assert serv_info.get_characteristic("Model").value == ""
    assert serv_info.get_characteristic("SerialNumber").value == "default"


def test_get_characteristic(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    acc = Accessory(mock_driver, "Test Accessory", aid=2)
    assert acc.available is True
    assert bridge.aid == 1
    assert bridge.get_characteristic(1, 2).display_name == "Identify"
    assert bridge.get_characteristic(2, 2) is None
    assert bridge.get_characteristic(3, 2) is None


def test_cannot_add_bridge_to_bridge(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    bridge2 = Bridge(mock_driver, "Test Bridge")
    with pytest.raises(ValueError):
        bridge.add_accessory(bridge2)


def test_to_hap_bridge(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    acc = Accessory(mock_driver, "Test Accessory", aid=2)
    assert acc.available is True
    bridge.add_accessory(acc)

    hap = bridge.to_HAP()
    assert hap == [
        {
            "aid": 1,
            "services": [
                {
                    "characteristics": [
                        {"format": "bool", "iid": 2, "perms": ["pw"], "type": "14"},
                        {
                            "format": "string",
                            "iid": 3,
                            "perms": ["pr"],
                            "type": "20",
                            "value": "",
                        },
                        {
                            "format": "string",
                            "iid": 4,
                            "perms": ["pr"],
                            "type": "21",
                            "value": "",
                        },
                        {
                            "format": "string",
                            "iid": 5,
                            "perms": ["pr"],
                            "type": "23",
                            "value": "Test Bridge",
                        },
                        {
                            "format": "string",
                            "iid": 6,
                            "perms": ["pr"],
                            "type": "30",
                            "value": "default",
                        },
                        {
                            "format": "string",
                            "iid": 7,
                            "perms": ["pr"],
                            "type": "52",
                            "value": "",
                        },
                    ],
                    "iid": 1,
                    "type": "3E",
                },
                {
                    "characteristics": [
                        {
                            "format": "string",
                            "iid": 9,
                            "perms": ["pr", "ev"],
                            "type": "37",
                            "value": "01.01.00",
                        }
                    ],
                    "iid": 8,
                    "type": "A2",
                },
            ],
        },
        {
            "aid": 2,
            "services": [
                {
                    "characteristics": [
                        {"format": "bool", "iid": 2, "perms": ["pw"], "type": "14"},
                        {
                            "format": "string",
                            "iid": 3,
                            "perms": ["pr"],
                            "type": "20",
                            "value": "",
                        },
                        {
                            "format": "string",
                            "iid": 4,
                            "perms": ["pr"],
                            "type": "21",
                            "value": "",
                        },
                        {
                            "format": "string",
                            "iid": 5,
                            "perms": ["pr"],
                            "type": "23",
                            "value": "Test Accessory",
                        },
                        {
                            "format": "string",
                            "iid": 6,
                            "perms": ["pr"],
                            "type": "30",
                            "value": "default",
                        },
                        {
                            "format": "string",
                            "iid": 7,
                            "perms": ["pr"],
                            "type": "52",
                            "value": "",
                        },
                    ],
                    "iid": 1,
                    "type": "3E",
                }
            ],
        },
    ]

    hap = acc.to_HAP()
    assert hap == {
        "aid": 2,
        "services": [
            {
                "characteristics": [
                    {
                        "format": "bool",
                        "iid": 2,
                        "perms": ["pw"],
                        "type": "14",
                    },
                    {
                        "format": "string",
                        "iid": 3,
                        "perms": ["pr"],
                        "type": "20",
                        "value": "",
                    },
                    {
                        "format": "string",
                        "iid": 4,
                        "perms": ["pr"],
                        "type": "21",
                        "value": "",
                    },
                    {
                        "format": "string",
                        "iid": 5,
                        "perms": ["pr"],
                        "type": "23",
                        "value": "Test Accessory",
                    },
                    {
                        "format": "string",
                        "iid": 6,
                        "perms": ["pr"],
                        "type": "30",
                        "value": "default",
                    },
                    {
                        "format": "string",
                        "iid": 7,
                        "perms": ["pr"],
                        "type": "52",
                        "value": "",
                    },
                ],
                "iid": 1,
                "type": "3E",
            }
        ],
    }
    bridge.get_characteristic(2, 2).display_name = "Custom Name Identify"
    hap = acc.to_HAP()
    assert hap == {
        "aid": 2,
        "services": [
            {
                "characteristics": [
                    {
                        "description": "Custom Name Identify",
                        "format": "bool",
                        "iid": 2,
                        "perms": ["pw"],
                        "type": "14",
                    },
                    {
                        "format": "string",
                        "iid": 3,
                        "perms": ["pr"],
                        "type": "20",
                        "value": "",
                    },
                    {
                        "format": "string",
                        "iid": 4,
                        "perms": ["pr"],
                        "type": "21",
                        "value": "",
                    },
                    {
                        "format": "string",
                        "iid": 5,
                        "perms": ["pr"],
                        "type": "23",
                        "value": "Test Accessory",
                    },
                    {
                        "format": "string",
                        "iid": 6,
                        "perms": ["pr"],
                        "type": "30",
                        "value": "default",
                    },
                    {
                        "format": "string",
                        "iid": 7,
                        "perms": ["pr"],
                        "type": "52",
                        "value": "",
                    },
                ],
                "iid": 1,
                "type": "3E",
            }
        ],
    }


def test_to_hap_standalone(mock_driver):
    acc = Accessory(mock_driver, "Test Accessory", aid=1)
    assert acc.available is True

    hap = acc.to_HAP()
    assert hap == {
        "aid": 1,
        "services": [
            {
                "characteristics": [
                    {"format": "bool", "iid": 2, "perms": ["pw"], "type": "14"},
                    {
                        "format": "string",
                        "iid": 3,
                        "perms": ["pr"],
                        "type": "20",
                        "value": "",
                    },
                    {
                        "format": "string",
                        "iid": 4,
                        "perms": ["pr"],
                        "type": "21",
                        "value": "",
                    },
                    {
                        "format": "string",
                        "iid": 5,
                        "perms": ["pr"],
                        "type": "23",
                        "value": "Test Accessory",
                    },
                    {
                        "format": "string",
                        "iid": 6,
                        "perms": ["pr"],
                        "type": "30",
                        "value": "default",
                    },
                    {
                        "format": "string",
                        "iid": 7,
                        "perms": ["pr"],
                        "type": "52",
                        "value": "",
                    },
                ],
                "iid": 1,
                "type": "3E",
            },
            {
                "characteristics": [
                    {
                        "format": "string",
                        "iid": 9,
                        "perms": ["pr", "ev"],
                        "type": "37",
                        "value": "01.01.00",
                    }
                ],
                "iid": 8,
                "type": "A2",
            },
        ],
    }


@pytest.mark.asyncio
async def test_bridge_run_stop():
    with patch(
        "pyhap.accessory_driver.HAPServer.async_stop", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.HAPServer.async_start", new_callable=AsyncMock
    ), patch(
        "pyhap.accessory_driver.AsyncZeroconf"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.persist"
    ), patch(
        "pyhap.accessory_driver.AccessoryDriver.load"
    ):
        driver = AccessoryDriver(loop=asyncio.get_event_loop())
        bridge = Bridge(driver, "Test Bridge")
        acc = TestAccessory(driver, "Test Accessory", aid=2)
        assert acc.available is True
        bridge.add_accessory(acc)
        acc2 = TestAccessory(driver, "Test Accessory 2")
        bridge.add_accessory(acc2)

        await bridge.run()
        await bridge.stop()
    assert acc.stopped is True
    assert acc2.stopped is True


def test_acc_with_(mock_driver):
    """Test ProgrammableSwitchEvent is always None."""
    acc = Accessory(mock_driver, "Test Accessory")
    serv_stateless_switch = acc.add_preload_service("StatelessProgrammableSwitch")
    char_doorbell_detected_switch = serv_stateless_switch.configure_char(
        "ProgrammableSwitchEvent",
        value=0,
        valid_values={"SinglePress": 0},
    )
    char_doorbell_detected_switch.client_update_value(0)
    assert char_doorbell_detected_switch.to_HAP()[HAP_REPR_VALUE] is None
    char_doorbell_detected_switch.client_update_value(None)
    assert char_doorbell_detected_switch.to_HAP()[HAP_REPR_VALUE] is None


def test_client_sends_invalid_value(mock_driver):
    """Test cleaning up invalid client value."""
    acc = Accessory(mock_driver, "Test Accessory")
    serv_switch = acc.add_preload_service("Switch")
    char_on = serv_switch.configure_char("On", value=False)
    # Client sends 1, but it should be True
    char_on.client_update_value(1)
    assert char_on.to_HAP()[HAP_REPR_VALUE] is True
