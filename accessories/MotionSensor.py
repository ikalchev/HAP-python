# An Accessory for a MotionSensor
import random

import RPi.GPIO as GPIO

from pyhap.accessory import Accessory
from pyhap.const import CATEGORY_SENSOR


class MotionSensor(Accessory):

    category = CATEGORY_SENSOR

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        serv_motion = self.add_preload_service('MotionSensor')
        self.char_detected = serv_motion.configure_char('MotionDetected')
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(7, GPIO.IN)
        GPIO.add_event_detect(7, GPIO.RISING, callback=self._detected)

    def _detected(self, _pin):
        self.char_detected.set_value(True)

    def stop(self):
        super().stop()
        GPIO.cleanup()
