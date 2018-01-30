"""
Tests for pyhap.accessory
"""
import pytest

import pyhap.accessory as accessory

class TestAccessory(object):

    def test_init(self):
        acc = accessory.Accessory("Test Accessory")

class TestBridge(TestAccessory):

    def test_init(self):
        bridge = accessory.Bridge("Test Bridge")

    def test_add_accessory(self):
        bridge = accessory.Bridge("Test Bridge")
        acc = accessory.Accessory("Test Accessory", aid=2)
        bridge.add_accessory(acc)

    def test_n_add_accessory_bridge_aid(self):
        bridge = accessory.Bridge("Test Bridge")
        acc = accessory.Accessory("Test Accessory", aid=accessory.STANDALONE_AID)
        with pytest.raises(ValueError):
            bridge.add_accessory(acc)

    def test_n_add_accessory_dup_aid(self):
        bridge = accessory.Bridge("Test Bridge")
        acc_1 = accessory.Accessory("Test Accessory 1", aid=2)
        acc_2 = accessory.Accessory("Test Accessory 2", aid=acc_1.aid)
        bridge.add_accessory(acc_1)
        with pytest.raises(ValueError):
            bridge.add_accessory(acc_2)