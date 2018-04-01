# An Accessory for the BMP180 sensor.
# This assumes the bmp180 module is in a package called sensors.
# Assume you have a bmp module with BMP180 class with read() method.
from sensors.bmp180 import BMP180 as sensor

import asyncio

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader


class BMP180(Accessory):

    category = Category.SENSOR

    def __init__(self, *args, **kwargs):
        super(BMP180, self).__init__(*args, **kwargs)

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")

        self.sensor = sensor()

    def _set_services(self):
        super(BMP180, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("TemperatureSensor"))

    def __getstate__(self):
        state = super(BMP180, self).__getstate__()
        state["sensor"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.sensor = sensor()

    async def run(self, stop_event, loop=None):
        while not stop_event.is_set():
            await asyncio.sleep(10)
            temp, _pressure = self.sensor.read()
            self.temp_char.set_value(temp)
