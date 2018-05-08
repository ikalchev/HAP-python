"""
Tests for pyhap.accessory_driver
"""
import asyncio
import os
import tempfile
from unittest.mock import patch, Mock

import pytest

from pyhap.accessory import (Accessory,
                             AsyncAccessory,
                             STANDALONE_AID)
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

@patch("pyhap.accessory_driver.HAPServer", new=Mock())
def test_persist_load():
    def get_acc():
        return Accessory("Test Accessory")
    fp = tempfile.NamedTemporaryFile(mode="r+")
    persist_file = fp.name
    fp.close()
    try:
        acc = get_acc()
        pk = acc.public_key
        # Create driver - state gets stored
        driver = AccessoryDriver(acc, 51234, persist_file=persist_file)
        # Re-start driver with a "new" accessory. State gets loaded into
        # the new accessory.
        del driver
        driver = AccessoryDriver(get_acc(), 51234, persist_file=persist_file)
        # Check pk is the same, i.e. that the state is indeed loaded.
        assert driver.accessory.public_key == pk
    finally:
        os.remove(persist_file)


@patch("pyhap.accessory_driver.Zeroconf", new=Mock())
@patch("pyhap.accessory_driver.AccessoryDriver.persist")
@patch("pyhap.accessory_driver.HAPServer", new=Mock())
def test_start_stop_sync_acc(_persist):
    class Acc(Accessory):
        running = True
        def run(self):
            while self.run_sentinel.wait(0):
                pass
            self.running = False
            driver.stop()
        def setup_message(self): pass

    acc = Acc("TestAcc")
    driver = AccessoryDriver(acc, 51234, persist_file="foo")
    driver.start()
    assert not acc.running


@patch("pyhap.accessory_driver.Zeroconf", new=Mock())
@patch("pyhap.accessory_driver.AccessoryDriver.persist")
@patch("pyhap.accessory_driver.HAPServer", new=Mock())
def test_start_stop_async_acc(_persist):
    class Acc(AsyncAccessory):
        @AsyncAccessory.run_at_interval(0)
        async def run(self):
            driver.stop()
        def setup_message(self): pass

    acc = Acc("TestAcc")
    driver = AccessoryDriver(acc, 51234, persist_file="foo")
    driver.start()
    assert driver.loop.is_closed()
