# An Accessory mocking a temperature sensor.
# It changes its value every few seconds.
import random
import time

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader

class TemperatureSensorAccessory(Accessory):

   category = Category.SENSOR

   def __init__(self, *args, **kwargs):
      super(TemperatureSensorAccessory, self).__init__(*args, **kwargs)

      self.temp_char = self.get_service("TemperatureSensor")\
                           .get_characteristic("CurrentTemperature")

   def _set_services(self):
      super(TemperatureSensorAccessory, self)._set_services()
      self.add_service(
         loader.get_serv_loader().get("TemperatureSensor"))

   def run(self):
      while True:
         self.temp_char.set_value(random.randint(18, 26))
         time.sleep(3)
