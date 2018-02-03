"""
Tests for pyhap.accessory_driver
"""
import pytest
from unittest.mock import patch, Mock

from pyhap.accessory import Accessory, STANDALONE_AID
from pyhap.accessory_driver import AccessoryDriver

@patch("pyhap.accessory_driver.AccessoryDriver.persist")
@patch("pyhap.accessory_driver.HAPServer", new=Mock())
def test_auto_add_aid_mac(_persist_mock):
    acc = Accessory("Test Accessory")
    _driver = AccessoryDriver(acc, 51234, "192.168.1.1", "test.accessory")
    assert acc.aid == STANDALONE_AID
    assert acc.mac is not None

@patch("pyhap.accessory_driver.AccessoryDriver.persist")
@patch("pyhap.accessory_driver.HAPServer", new=Mock())
def test_not_standalone_aid(_persist_mock):
    acc = Accessory("Test Accessory", aid=STANDALONE_AID + 1)
    with pytest.raises(ValueError):
        _driver = AccessoryDriver(acc, 51234, "192.168.1.1", "test.accessory")