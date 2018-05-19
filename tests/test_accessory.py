"""Tests for pyhap.accessory."""
import pytest

from pyhap.accessory import Accessory, Bridge
from pyhap.const import STANDALONE_AID


# #### Accessory ######
# execute with `-k acc`
# #####################

def test_acc_init(mock_driver):
    Accessory(mock_driver, 'Test Accessory')


def test_acc_publish_no_broker(mock_driver):
    acc = Accessory(mock_driver, 'Test Accessory')
    service = acc.driver.loader.get_service('TemperatureSensor')
    char = service.get_characteristic('CurrentTemperature')
    acc.add_service(service)
    char.set_value(25, should_notify=True)


def test_acc_set_services_compatible(mock_driver):
    """Test deprecated method _set_services."""
    class Acc(Accessory):
        def _set_services(self):
            super()._set_services()
            serv = self.driver.loader.get_service('TemperatureSensor')
            self.add_service(serv)
    acc = Acc(mock_driver, 'Test Accessory')
    assert acc.get_service('AccessoryInformation') is not None
    assert acc.get_service('TemperatureSensor') is not None


# #### Bridge ############
# execute with `-k bridge`
# ########################

def test_bridge_init(mock_driver):
    Bridge(mock_driver, 'Test Bridge')


def test_bridge_add_accessory(mock_driver):
    bridge = Bridge(mock_driver, 'Test Bridge')
    acc = Accessory(mock_driver, 'Test Accessory', aid=2)
    bridge.add_accessory(acc)
    acc2 = Accessory(mock_driver, 'Test Accessory 2')
    bridge.add_accessory(acc2)
    assert acc2.aid != STANDALONE_AID and acc2.aid != acc.aid


def test_bridge_n_add_accessory_bridge_aid(mock_driver):
    bridge = Bridge(mock_driver, 'Test Bridge')
    acc = Accessory(mock_driver, 'Test Accessory', aid=STANDALONE_AID)
    with pytest.raises(ValueError):
        bridge.add_accessory(acc)


def test_bridge_n_add_accessory_dup_aid(mock_driver):
    bridge = Bridge(mock_driver, 'Test Bridge')
    acc_1 = Accessory(mock_driver, 'Test Accessory 1', aid=2)
    acc_2 = Accessory(mock_driver, 'Test Accessory 2', aid=acc_1.aid)
    bridge.add_accessory(acc_1)
    with pytest.raises(ValueError):
        bridge.add_accessory(acc_2)
