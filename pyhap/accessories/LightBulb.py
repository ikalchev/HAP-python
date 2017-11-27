# An Accessory for a LED attached to pin 11.
import logging

import RPi.GPIO as GPIO

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader


class LightBulb(Accessory):

    category = Category.LIGHTBULB

    @classmethod
    def _gpio_setup(_cls, pin):
        if GPIO.getmode() is None:
            GPIO.setmode(GPIO.BOARD)
        GPIO.setup(pin, GPIO.OUT)

    def __init__(self, *args, pin=11, **kwargs):
        super(LightBulb, self).__init__(*args, **kwargs)
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
        super(LightBulb, self)._set_services()

        bulb_service = loader.get_serv_loader().get("Lightbulb")
        self.add_service(bulb_service)
        bulb_service.get_characteristic("On").setter_callback = self.set_bulb

    def stop(self):
        super(LightBulb, self).stop()
        GPIO.cleanup()
