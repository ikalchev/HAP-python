"""Tests for pyhap.accessory."""
from unittest.mock import patch

import pytest

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import STANDALONE_AID

with patch('pyhap.accessory_driver.HAPServer'), \
        patch('pyhap.accessory_driver.Zeroconf'):
    DRIVER = AccessoryDriver()


# #### Accessory ####
# ###################

def test_init():
    Accessory(DRIVER, 'Test Accessory')


def test_publish_no_broker():
    acc = Accessory(DRIVER, 'Test Accessory')
    service = acc.driver.loader.get_service('TemperatureSensor')
    char = service.get_characteristic('CurrentTemperature')
    acc.add_service(service)
    char.set_value(25, should_notify=True)


def test_set_services_compatible():
    """Test deprecated method _set_services."""
    class Acc(Accessory):
        def _set_services(self):
            super()._set_services()
            serv = self.driver.loader.get_service('TemperatureSensor')
            self.add_service(serv)
    acc = Acc(DRIVER, 'Test Accessory')
    assert acc.get_service('AccessoryInformation') is not None
    assert acc.get_service('TemperatureSensor') is not None


# #### Bridge ####
# ################

def test_init_bridge():
    Bridge(DRIVER, 'Test Bridge')


def test_add_accessory():
    bridge = Bridge(DRIVER, 'Test Bridge')
    acc = Accessory(DRIVER, 'Test Accessory', aid=2)
    bridge.add_accessory(acc)
    acc2 = Accessory(DRIVER, 'Test Accessory 2')
    bridge.add_accessory(acc2)
    assert acc2.aid != STANDALONE_AID and acc2.aid != acc.aid


def test_n_add_accessory_bridge_aid():
    bridge = Bridge(DRIVER, 'Test Bridge')
    acc = Accessory(DRIVER, 'Test Accessory', aid=STANDALONE_AID)
    with pytest.raises(ValueError):
        bridge.add_accessory(acc)


def test_n_add_accessory_dup_aid():
    bridge = Bridge(DRIVER, 'Test Bridge')
    acc_1 = Accessory(DRIVER, 'Test Accessory 1', aid=2)
    acc_2 = Accessory(DRIVER, 'Test Accessory 2', aid=acc_1.aid)
    bridge.add_accessory(acc_1)
    with pytest.raises(ValueError):
        bridge.add_accessory(acc_2)
