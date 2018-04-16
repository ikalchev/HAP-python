# An Accessory for the TSL2591 digital light sensor.
# The TSL2591.py module was taken from https://github.com/maxlklaxl/python-tsl2591

import tsl2591

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR
import pyhap.loader as loader


class TSL2591(Accessory):
    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.lux_char = self.get_service("LightSensor") \
            .get_characteristic("CurrentAmbientLightLevel")

        self.tsl = tsl2591.Tsl2591()

    def _set_services(self):
        super()._set_services()
        self.add_service(
            loader.get_serv_loader().get_service("LightSensor"))

    def __getstate__(self):
        state = super().__getstate__()
        state["tsl"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.tsl = tsl2591.Tsl2591()

    def run(self):
        while not self.run_sentinel.wait(10):
            full, ir = self.tsl.get_full_luminosity()
            lux = min(max(0.001, self.tsl.calculate_lux(full, ir)), 10000)
            self.lux_char.set_value(lux)

