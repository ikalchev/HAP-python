# An Accessory for the TSL2591 digital light sensor.
# The TSL2591.py module was taken from https://github.com/maxlklaxl/python-tsl2591

import tsl2591

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR


class TSL2591(Accessory):
    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_light = self.add_preload_service('LightSensor')
        self.char_lux = serv_light.configure_char('CurrentAmbientLightLevel')

        self.tsl = tsl2591.Tsl2591()

    def __getstate__(self):
        state = super().__getstate__()
        state["tsl"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.tsl = tsl2591.Tsl2591()

    @Accessory.run_at_interval(10)
    def run(self):
        full, ir = self.tsl.get_full_luminosity()
        lux = min(max(0.001, self.tsl.calculate_lux(full, ir)), 10000)
        self.char_lux.set_value(lux)
