# An Accessory for the BMP180 sensor.
# This assumes the bmp180 module is in a package called sensors.
# Assume you have a bmp module with BMP180 class with read() method.
from sensors.bmp180 import BMP180 as sensor

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR


class BMP180(Accessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_temp = self.add_preload_service('TemperatureSensor')
        self.char_temp = serv_temp.get_characteristic('CurrentTemperature')

        self.sensor = sensor()

    def __getstate__(self):
        state = super().__getstate__()
        state['sensor'] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.sensor = sensor()

    @Accessory.run_at_interval(30)
    def run(self):
        temp, _pressure = self.sensor.read()
        self.char_temp.set_value(temp)
