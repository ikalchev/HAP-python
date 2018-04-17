# An Accessory for a MotionSensor
import random

import RPi.GPIO as GPIO

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR
import pyhap.loader as loader


class MotionSensor(Accessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.detected_char = self.get_service("MotionSensor")\
                                 .get_characteristic("MotionDetected")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(7, GPIO.IN)
        GPIO.add_event_detect(7, GPIO.RISING, callback=self._detected)

    def _set_services(self):
        super()._set_services()
        self.add_service(
            loader.get_serv_loader().get_service("MotionSensor"))

    def _detected(self, _pin):
        self.detected_char.set_value(True)

    def stop(self):
        super().stop()
        GPIO.cleanup()
