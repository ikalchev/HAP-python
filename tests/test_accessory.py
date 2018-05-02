"""
Tests for pyhap.accessory
"""
import pytest

import pyhap.accessory as accessory
import pyhap.loader as loader

class TestAccessory(object):

    def test_init(self):
        acc = accessory.Accessory('Test Accessory')

    def test_publish_no_broker(self):
        acc = accessory.Accessory('Test Accessory')
        service = loader.get_serv_loader().get_service('TemperatureSensor')
        char = service.get_characteristic('CurrentTemperature')
        acc.add_service(service)
        char.set_value(25, should_notify=True)

    def test_set_services_compatible(self):
        """Test that _set_services still works and has access to the info services"""
        class A(accessory.Accessory):
            def _set_services(self):
                super()._set_services()
                s = loader.get_serv_loader().get_service("TemperatureSensor")
                self.add_service(s)
                assert self.get_service("AccessoryInformation") is not None
        a = A("Test Accessory")
        assert a.get_service("TemperatureSensor") is not None

class TestBridge(TestAccessory):

    def test_init(self):
        bridge = accessory.Bridge('Test Bridge')

    def test_add_accessory(self):
        bridge = accessory.Bridge('Test Bridge')
        acc = accessory.Accessory('Test Accessory', aid=2)
        bridge.add_accessory(acc)
        acc2 = accessory.Accessory('Test Accessory 2')
        bridge.add_accessory(acc2)
        assert (acc2.aid != accessory.STANDALONE_AID
                and acc2.aid != acc.aid)

    def test_n_add_accessory_bridge_aid(self):
        bridge = accessory.Bridge('Test Bridge')
        acc = accessory.Accessory('Test Accessory', aid=accessory.STANDALONE_AID)
        with pytest.raises(ValueError):
            bridge.add_accessory(acc)

    def test_n_add_accessory_dup_aid(self):
        bridge = accessory.Bridge('Test Bridge')
        acc_1 = accessory.Accessory('Test Accessory 1', aid=2)
        acc_2 = accessory.Accessory('Test Accessory 2', aid=acc_1.aid)
        bridge.add_accessory(acc_1)
        with pytest.raises(ValueError):
            bridge.add_accessory(acc_2)