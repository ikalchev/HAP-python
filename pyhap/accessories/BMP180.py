# An Accessory for the BMP180 sensor.
# This assumes the bmp180 module is in a package called sensors.
# Assume you have a bmp module with BMP180 class with read() method.
from sensors.bmp180 import BMP180 as sensor

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR
import pyhap.loader as loader


class BMP180(Accessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")

        self.sensor = sensor()

    def _set_services(self):
        super()._set_services()
        self.add_service(
            loader.get_serv_loader().get_service("TemperatureSensor"))

    def __getstate__(self):
        state = super().__getstate__()
        state["sensor"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.sensor = sensor()

    def run(self):
        while not self.run_sentinel.wait(30):
            temp, _pressure = self.sensor.read()
            self.temp_char.set_value(temp)
