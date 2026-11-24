"""Starts a bridge with a lock that refuses commands while its bolt moves,
and a sensor that is offline, both answering with a HAP status.
"""
import logging
import signal
import time

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import (
    CATEGORY_DOOR_LOCK,
    CATEGORY_SENSOR,
    HAP_SERVER_STATUS,
)
from pyhap.errors import HapStatusError


logging.basicConfig(level=logging.INFO, format="[%(module)s] %(message)s")

LOCK_SECURED = 1
LOCK_UNSECURED = 0


class BusyLock(Accessory):
    """A lock whose bolt takes a few seconds to reach its end.

    While it is moving, a controller's command to change the target state
    is refused with a HAP status instead of the generic failure: the Home
    app tells that the lock is busy, and does not pretend the command
    worked. The refused target is put back, so the lock does not promise
    a state it will not reach.
    """

    category = CATEGORY_DOOR_LOCK

    MOVE_SECONDS = 5

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._moving_until = 0.0
        service = self.add_preload_service("LockMechanism")
        self.char_current_state = service.configure_char(
            "LockCurrentState", value=LOCK_SECURED)
        self.char_target_state = service.configure_char(
            "LockTargetState", value=LOCK_SECURED,
            setter_callback=self.set_target_state)
        self.set_info_service(serial_number="busy lock")

    @Accessory.run_at_interval(1)
    async def run(self):
        """End a move once the bolt reached its end."""
        if time.monotonic() >= self._moving_until:
            if self.char_current_state.value != self.char_target_state.value:
                logging.info("The bolt reached its end")
                self.char_current_state.set_value(self.char_target_state.value)

    def set_target_state(self, value):
        """Accept a command, or refuse it while the bolt is still moving."""
        if time.monotonic() < self._moving_until:
            # The command came while the bolt has not reached its end. Answer
            # with the resource busy status, and take the refused target back
            # so the lock's own state stays consistent.
            self.char_target_state.set_value(
                self.char_current_state.value, should_notify=False)
            logging.info("Refusing to move the bolt again, it is still moving")
            raise HapStatusError(HAP_SERVER_STATUS.RESOURCE_BUSY)

        logging.info("Moving the bolt to %s", "secure" if value else "unsecure")
        self._moving_until = time.monotonic() + self.MOVE_SECONDS


class OfflineSensor(Accessory):
    """A temperature sensor that is currently unreachable.

    Its getter raises a HAP status, so reads answer with that status
    instead of a value that would be a lie. When it is back online,
    raise nothing and return the temperature.
    """

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_temp = self.add_preload_service("TemperatureSensor")
        self.char_temp = serv_temp.configure_char(
            'CurrentTemperature', getter_callback=self.get_temperature)

    def get_temperature(self):
        """Answer with a status while the sensor cannot be reached."""
        logging.info("The temperature cannot be read right now")
        raise HapStatusError(HAP_SERVER_STATUS.SERVICE_COMMUNICATION_FAILURE)


def get_bridge(driver):
    bridge = Bridge(driver, 'Bridge')

    bridge.add_accessory(BusyLock(driver, 'Busy Lock'))
    bridge.add_accessory(OfflineSensor(driver, 'Offline Sensor'))

    return bridge


driver = AccessoryDriver(port=51826, persist_file='busy_lock.state')
driver.add_accessory(accessory=get_bridge(driver))
signal.signal(signal.SIGTERM, driver.signal_handler)
driver.start()
