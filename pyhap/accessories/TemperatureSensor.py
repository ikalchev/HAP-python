# An Accessory mocking a temperature sensor.
# It changes its value every few seconds.
import random
import time

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader


class TemperatureSensor(Accessory):

    category = Category.SENSOR

    def __init__(self, *args, **kwargs):
        super(TemperatureSensor, self).__init__(*args, **kwargs)

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")

    def _set_services(self):
        super(TemperatureSensor, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("TemperatureSensor"))

    def run(self):
        while not self.run_sentinel.wait(3):
            self.temp_char.set_value(random.randint(18, 26))
