"""An example of how to setup and start an Accessory.

This is:
1. Create the Accessory object you want.
2. Add it to an AccessoryDriver, which will advertise it on the local network,
    setup a server to answer client querries, etc.
"""
import logging
import os
import pickle
import signal

import pyhap.util as util
from pyhap.accessories.TemperatureSensor import TemperatureSensor
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver

logging.basicConfig(level=logging.INFO)


def get_bridge():
    """Call this method to get a Bridge instead of a standalone accessory."""
    bridge = Bridge(display_name="Bridge", pincode=b"203-23-999")
    temp_sensor = TemperatureSensor("Termometer")
    temp_sensor2 = TemperatureSensor("Termometer2")
    bridge.add_accessory(temp_sensor)
    bridge.add_accessory(temp_sensor2)

    # Uncomment if you have RPi module and want a LED LightBulb service on pin 16.
    # from pyhap.accessories.LightBulb import LightBulb
    # bulb = LightBulb("Desk LED", pin=16)
    # bridge.add_accessory(bulb)
    return bridge

def get_accessory():
    """Call this method to get a standalone Accessory."""
    acc = TemperatureSensor("MyTempSensor",
                            pincode=b"203-23-999",
                            mac=util.generate_mac())
    return acc


# The AccessoryDriver preserves the state of the accessory
# (by default, in the below file), so that you can restart it without pairing again.
if os.path.exists("accessory.pickle"):
    with open("accessory.pickle", "rb") as f:
        acc = pickle.load(f)
else:
    acc = get_accessory()  # Change to get_bridge() if you want to run a Bridge.

# Start the accessory on port 51826
driver = AccessoryDriver(acc, 51826)
# We want KeyboardInterrupts and SIGTERM (kill) to be handled by the driver itself,
# so that it can gracefully stop the accessory, server and advertising.
signal.signal(signal.SIGINT, driver.signal_handler)
signal.signal(signal.SIGTERM, driver.signal_handler)
# Start it!
driver.start()
