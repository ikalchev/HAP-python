"""Tests for pyhap.accessory."""
from io import StringIO
from unittest.mock import patch

import pytest

from pyhap import accessory
from pyhap.accessory import Accessory, Bridge
from pyhap.const import (
    CATEGORY_CAMERA,
    CATEGORY_TARGET_CONTROLLER,
    CATEGORY_TELEVISION,
    STANDALONE_AID,
)
from pyhap.service import Service
from pyhap.state import State

from . import AsyncMock

# #### Accessory ######
# execute with `-k acc`
# #####################


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
    assert acc2.aid != STANDALONE_AID and acc2.aid != acc.aid


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


def test_to_hap(mock_driver):
    bridge = Bridge(mock_driver, "Test Bridge")
    acc = Accessory(mock_driver, "Test Accessory", aid=2)
    assert acc.available is True
    bridge.add_accessory(acc)

    assert bridge.to_HAP() == [
        {
            "aid": 1,
            "services": [
                {
                    "iid": 1,
                    "type": "0000003E-0000-1000-8000-0026BB765291",
                    "characteristics": [
                        {
                            "iid": 2,
                            "type": "00000014-0000-1000-8000-0026BB765291",
                            "description": "Identify",
                            "perms": ["pw"],
                            "format": "bool",
                        },
                        {
                            "iid": 3,
                            "type": "00000020-0000-1000-8000-0026BB765291",
                            "description": "Manufacturer",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "",
                        },
                        {
                            "iid": 4,
                            "type": "00000021-0000-1000-8000-0026BB765291",
                            "description": "Model",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "",
                        },
                        {
                            "iid": 5,
                            "type": "00000023-0000-1000-8000-0026BB765291",
                            "description": "Name",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "Test Bridge",
                        },
                        {
                            "iid": 6,
                            "type": "00000030-0000-1000-8000-0026BB765291",
                            "description": "SerialNumber",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "default",
                        },
                        {
                            "iid": 7,
                            "type": "00000052-0000-1000-8000-0026BB765291",
                            "description": "FirmwareRevision",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "",
                        },
                    ],
                }
            ],
        },
        {
            "aid": 2,
            "services": [
                {
                    "iid": 1,
                    "type": "0000003E-0000-1000-8000-0026BB765291",
                    "characteristics": [
                        {
                            "iid": 2,
                            "type": "00000014-0000-1000-8000-0026BB765291",
                            "description": "Identify",
                            "perms": ["pw"],
                            "format": "bool",
                        },
                        {
                            "iid": 3,
                            "type": "00000020-0000-1000-8000-0026BB765291",
                            "description": "Manufacturer",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "",
                        },
                        {
                            "iid": 4,
                            "type": "00000021-0000-1000-8000-0026BB765291",
                            "description": "Model",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "",
                        },
                        {
                            "iid": 5,
                            "type": "00000023-0000-1000-8000-0026BB765291",
                            "description": "Name",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "Test Accessory",
                        },
                        {
                            "iid": 6,
                            "type": "00000030-0000-1000-8000-0026BB765291",
                            "description": "SerialNumber",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "default",
                        },
                        {
                            "iid": 7,
                            "type": "00000052-0000-1000-8000-0026BB765291",
                            "description": "FirmwareRevision",
                            "perms": ["pr"],
                            "format": "string",
                            "value": "",
                        },
                    ],
                }
            ],
        },
    ]
    assert acc.to_HAP() == {
        "aid": 2,
        "services": [
            {
                "iid": 1,
                "type": "0000003E-0000-1000-8000-0026BB765291",
                "characteristics": [
                    {
                        "iid": 2,
                        "type": "00000014-0000-1000-8000-0026BB765291",
                        "description": "Identify",
                        "perms": ["pw"],
                        "format": "bool",
                    },
                    {
                        "iid": 3,
                        "type": "00000020-0000-1000-8000-0026BB765291",
                        "description": "Manufacturer",
                        "perms": ["pr"],
                        "format": "string",
                        "value": "",
                    },
                    {
                        "iid": 4,
                        "type": "00000021-0000-1000-8000-0026BB765291",
                        "description": "Model",
                        "perms": ["pr"],
                        "format": "string",
                        "value": "",
                    },
                    {
                        "iid": 5,
                        "type": "00000023-0000-1000-8000-0026BB765291",
                        "description": "Name",
                        "perms": ["pr"],
                        "format": "string",
                        "value": "Test Accessory",
                    },
                    {
                        "iid": 6,
                        "type": "00000030-0000-1000-8000-0026BB765291",
                        "description": "SerialNumber",
                        "perms": ["pr"],
                        "format": "string",
                        "value": "default",
                    },
                    {
                        "iid": 7,
                        "type": "00000052-0000-1000-8000-0026BB765291",
                        "description": "FirmwareRevision",
                        "perms": ["pr"],
                        "format": "string",
                        "value": "",
                    },
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_bridge_run_stop(mock_driver):
    mock_driver.async_add_job = AsyncMock()
    bridge = Bridge(mock_driver, "Test Bridge")
    acc = Accessory(mock_driver, "Test Accessory", aid=2)
    assert acc.available is True
    bridge.add_accessory(acc)
    acc2 = Accessory(mock_driver, "Test Accessory 2")
    bridge.add_accessory(acc2)

    await bridge.run()
    assert mock_driver.async_add_job.called
    await bridge.stop()
