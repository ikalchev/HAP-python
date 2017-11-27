# An Accessory for a MotionSensor
import random

import RPi.GPIO as GPIO

from pyhap.accessory import Accessory, Category
import pyhap.loader as loader


class MotionSensor(Accessory):

    category = Category.SENSOR

    def __init__(self, *args, **kwargs):
        super(MotionSensor, self).__init__(*args, **kwargs)

        self.detected_char = self.get_service("MotionSensor")\
                                 .get_characteristic("MotionDetected")
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(7, GPIO.IN)
        GPIO.add_event_detect(7, GPIO.RISING, callback=self._detected)

    def _set_services(self):
        super(MotionSensor, self)._set_services()
        self.add_service(
            loader.get_serv_loader().get("MotionSensor"))

    def _detected(self, _pin):
        self.detected_char.set_value(True)

    def stop(self):
        super(MotionSensor, self).stop()
        GPIO.cleanup()
