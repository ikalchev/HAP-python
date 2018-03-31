"""An example of how to setup and start an Accessory.

This is:
1. Create the Accessory object you want.
2. Add it to an AccessoryDriver, which will advertise it on the local network,
    setup a server to answer client queries, etc.
"""
import logging
import signal
import time
import random

from pyhap.accessories.TemperatureSensor import TemperatureSensor
from pyhap.accessory import Bridge, Accessory, Category
from pyhap.accessory_driver import AccessoryDriver
import pyhap.loader as loader

logging.basicConfig(level=logging.DEBUG)


class SyncTemperatureSensor(Accessory):

    category = Category.SENSOR

    def __init__(self, *args, **kwargs):
        super(SyncTemperatureSensor, self).__init__(*args, **kwargs)

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")

    def _set_services(self):
        super(SyncTemperatureSensor, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("TemperatureSensor"))

    def run(self, stop_event, loop=None):
        while not stop_event.is_set():  # This is not being set because it is from another thread.
            time.sleep(3)
            self.temp_char.set_value(random.randint(18, 26))
            print(self.display_name, self.temp_char.value)


def get_bridge():
    """Call this method to get a Bridge instead of a standalone accessory."""
    bridge = Bridge(display_name="Bridge")
    temp_sensor = TemperatureSensor("Termometer")
    temp_sensor2 = SyncTemperatureSensor("Termometer2")
    bridge.add_accessory(temp_sensor)
    bridge.add_accessory(temp_sensor2)

    # Uncomment if you have RPi module and want a LED LightBulb service on pin 16.
    # from pyhap.accessories.LightBulb import LightBulb
    # bulb = LightBulb("Desk LED", pin=16)
    # bridge.add_accessory(bulb)
    return bridge


def get_accessory():
    """Call this method to get a standalone Accessory."""
    acc = TemperatureSensor("MyTempSensor")
    return acc


acc = get_bridge()  # Change to get_bridge() if you want to run a Bridge.

# Start the accessory on port 51826
driver = AccessoryDriver(acc, port=51826)
# We want KeyboardInterrupts and SIGTERM (kill) to be handled by the driver itself,
# so that it can gracefully stop the accessory, server and advertising.
signal.signal(signal.SIGINT, driver.signal_handler)
signal.signal(signal.SIGTERM, driver.signal_handler)
# Start it!
driver.start()
