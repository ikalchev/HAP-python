# An Accessory for a LED attached to pin 11.
import logging

import RPi.GPIO as GPIO

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_LIGHTBULB
import pyhap.loader as loader


class LightBulb(Accessory):

    category = CATEGORY_LIGHTBULB

    @classmethod
    def _gpio_setup(_cls, pin):
        if GPIO.getmode() is None:
            GPIO.setmode(GPIO.BOARD)
        GPIO.setup(pin, GPIO.OUT)

    def __init__(self, *args, pin=11, **kwargs):
        super().__init__(*args, **kwargs)
        self.pin = pin
        self._gpio_setup(pin)

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._gpio_setup(self.pin)

    def set_bulb(self, value):
        if value:
            GPIO.output(self.pin, GPIO.HIGH)
        else:
            GPIO.output(self.pin, GPIO.LOW)

    def _set_services(self):
        super()._set_services()

        bulb_service = loader.get_serv_loader().get_service("Lightbulb")
        self.add_service(bulb_service)
        bulb_service.get_characteristic("On").setter_callback = self.set_bulb

    def stop(self):
        super().stop()
        GPIO.cleanup()
