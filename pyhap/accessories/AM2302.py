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

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader


class AM2302(Accessory):

    category = Category.SENSOR

    def __init__(self, *args, pin=4, **kwargs):
        super(AM2302, self).__init__(*args, **kwargs)
        self.pin = pin

        self.temp_char = self.get_service("TemperatureSensor")\
                             .get_characteristic("CurrentTemperature")

        self.humidity_char = self.get_service("HumiditySensor")\
                                 .get_characteristic("CurrentRelativeHumidity")

        self.sensor = DHT22.sensor(pigpio.pi(), pin)

    def _set_services(self):
        super(AM2302, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("TemperatureSensor"))
        self.add_service(
            loader.get_serv_loader().get("HumiditySensor"))

    def __getstate__(self):
        state = super(AM2302, self).__getstate__()
        state["sensor"] = None
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.sensor = DHT22.sensor(pigpio.pi(), self.pin)

    def run(self):
        while not self.run_sentinel.wait(10):
            self.sensor.trigger()
            time.sleep(0.2)
            t = self.sensor.temperature()
            h = self.sensor.humidity()
            self.temp_char.set_value(t)
            self.humidity_char.set_value(h)
