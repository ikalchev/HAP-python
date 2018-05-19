"""Tests for pyhap.accessory_driver."""
import tempfile
from unittest.mock import patch

import pytest

from pyhap.accessory import Accessory, AsyncAccessory, STANDALONE_AID
from pyhap.accessory_driver import AccessoryDriver


@pytest.fixture
def driver():
    with patch('pyhap.accessory_driver.HAPServer'), \
        patch('pyhap.accessory_driver.Zeroconf'), \
            patch('pyhap.accessory_driver.AccessoryDriver.persist'):
        yield AccessoryDriver()


def test_auto_add_aid_mac(driver):
    acc = Accessory(driver, 'Test Accessory')
    driver.add_accessory(acc)
    assert acc.aid == STANDALONE_AID
    assert driver.state.mac is not None


def test_not_standalone_aid(driver):
    acc = Accessory(driver, 'Test Accessory', aid=STANDALONE_AID + 1)
    with pytest.raises(ValueError):
        driver.add_accessory(acc)


def test_persist_load():
    with tempfile.NamedTemporaryFile(mode='r+') as file:
        with patch('pyhap.accessory_driver.HAPServer'), \
                patch('pyhap.accessory_driver.Zeroconf'):
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.persist()
            pk = driver.state.public_key
            # Re-start driver with a "new" accessory. State gets loaded into
            # the new accessory.
            driver = AccessoryDriver(port=51234, persist_file=file.name)
            driver.load()
    assert driver.state.public_key == pk


def test_start_stop_sync_acc(driver):
    class Acc(Accessory):
        running = True

        def run(self):
            while self.driver.stop_event.wait(0):
                pass
            self.running = False
            driver.stop()

        def setup_message(self):
            pass

    acc = Acc(driver, 'TestAcc')
    driver.add_accessory(acc)
    driver.start()
    assert not acc.running


def test_start_stop_async_acc(driver):
    class Acc(AsyncAccessory):

        @AsyncAccessory.run_at_interval(0)
        async def run(self):
            driver.stop()

        def setup_message(self):
            pass

    acc = Acc(driver, 'TestAcc')
    driver.add_accessory(acc)
    driver.start()
    assert driver.loop.is_closed()
