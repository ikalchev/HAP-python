# An Accessory mocking a temperature sensor.
# It changes its value every few seconds.
import asyncio
import random
import time

from pyhap.accessory import AsyncAccessory, Category
import pyhap.loader as loader

class TemperatureSensor(AsyncAccessory):

    category = Category.SENSOR

    def __init__(self, *args, **kwargs):
        super(TemperatureSensor, self).__init__(*args, **kwargs)

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")

    def _set_services(self):
        super(TemperatureSensor, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("TemperatureSensor"))

    @AsyncAccessory.run_at_interval(3)
    async def run(self):
        self.temp_char.set_value(random.randint(18, 26))
        print(self.display_name, self.temp_char.value)
