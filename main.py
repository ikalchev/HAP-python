import logging
import os
import binascii
import pickle

from zeroconf import Zeroconf

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
   acc =TemperatureSensorAccessory.create(display_name="pyhap", pincode=b"203-23-999")
   return acc

if os.path.exists("accessory.pickle"):
   with open("accessory.pickle", "rb") as f:
      acc = pickle.load(f)
else:
   acc = get_accessory()

advertiser = Zeroconf()
driver = AccessoryDriver(acc, advertiser, 51826)

driver.start()

while True:
   try:
      pass
   except KeyboardInterrupt:
      driver.stop()
      advertiser.unregister_all_services()
      break
