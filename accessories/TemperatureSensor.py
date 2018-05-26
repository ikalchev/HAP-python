# An Accessory mocking a temperature sensor.
# It changes its value every few seconds.
import random

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR


class TemperatureSensor(Accessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_temp = self.add_preload_service('TemperatureSensor')
        self.char_temp = serv_temp.configure_char('CurrentTemperature')

    @Accessory.run_at_interval(3)
    def run(self):
        self.char_temp.set_value(random.randint(18, 26))
