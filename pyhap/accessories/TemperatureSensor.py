# An Accessory mocking a temperature sensor.
# It changes its value every few seconds.
import asyncio
import random
import time

from pyhap.accessory import AsyncAccessory
from pyhap.const import CATEGORY_SENSOR


class TemperatureSensor(AsyncAccessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_temp = self.add_preload_service('TemperatureSensor')
        self.char_temp = serv_temp.configure_char('CurrentTemperature')

    @AsyncAccessory.run_at_interval(3)
    async def run(self):
        self.char_temp.set_value(random.randint(18, 26))
