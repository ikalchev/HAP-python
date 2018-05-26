"""An Accessory for the AM2302 temperature and humidity sensor.
Assumes the DHT22 module is in a package called sensors.
Also, make sure pigpiod is running.
The DHT22 module was taken from
https://www.raspberrypi.org/forums/viewtopic.php?f=37&t=71336
"""
import time
import random

import pigpio
import sensors.DHT22 as DHT22

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR


class AM2302(Accessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, pin=4, **kwargs):
        super().__init__(*args, **kwargs)
        self.pin = pin

        serv_temp = self.add_preload_service('TemperatureSensor')
        serv_humidity = self.add_preload_service('HumiditySensor')

        self.char_temp = serv_temp.get_characteristic('CurrentTemperature')
        self.char_humidity = serv_humidity \
            .get_characteristic('CurrentRelativeHumidity')

        self.sensor = DHT22.sensor(pigpio.pi(), pin)

    def __getstate__(self):
        state = super().__getstate__()
        state['sensor'] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.sensor = DHT22.sensor(pigpio.pi(), self.pin)

    @Accessory.run_at_interval(10)
    def run(self):
        self.sensor.trigger()
        time.sleep(0.2)
        t = self.sensor.temperature()
        h = self.sensor.humidity()
        self.char_temp.set_value(t)
        self.char_humidity.set_value(h)
