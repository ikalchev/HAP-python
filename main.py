import logging
import os
import pickle
import signal

import pyhap.util as util
from pyhap.accessories.TemperatureSensor import TemperatureSensorAccessory
from pyhap.accessory import Bridge
from pyhap.accessory_driver import AccessoryDriver

logging.basicConfig(level=logging.INFO)

def get_accessory():
   # Example of a Bridged accessories.
   #bridge = Bridge(display_name="Bridge",
   #                mac=util.generate_mac(),
   #                pincode=b"203-23-999")
   #temp_sensor = BMP180_Accessory("BMP180")
   #bulb = LightBulb("Desk LED", pin=16)
   #
   #bridge.add_accessory(temp_sensor)
   #bridge.add_accessory(bulb)
   #return bridge

   # Standalone accessory
   # Displayed name will be "test"
   acc = TemperatureSensorAccessory.create("test", pincode=b"203-23-999")
   return acc

# The AccessoryDriver preserves the state of the accessory
# (by default, in the below file), so that you can restart it without pairing again.
if os.path.exists("accessory.pickle"):
   with open("accessory.pickle", "rb") as f:
      acc = pickle.load(f)
else:
   acc = get_accessory()

# Start the accessory on port 51826
driver = AccessoryDriver(acc, 51826)
# We want KeyboardInterrupts to be handled by the driver itself, for convenience.
signal.signal(signal.SIGINT, driver.signal_handler)
# Start it!
driver.start()
